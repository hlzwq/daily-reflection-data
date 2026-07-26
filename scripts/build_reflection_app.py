#!/usr/bin/env python3
"""
Scan -A Daily vault (rolling month window) → reflections.json + offline js + HTML.

Schema v2: days[].docs[] multi-document per day.
Inclusion rules live in feed.config.json (filename patterns).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "feed.config.json"
CST = timezone(timedelta(hours=8))

DEFAULT_CONFIG = {
    "vault_root": "..",
    "month_dir_regex": r"^\d{4}-\d{1,2}$",
    "window": {"mode": "rolling", "months": 2},
    "inclusion": {
        "require_date_prefix": True,
        "patterns": [
            {"pattern": "*-每日反思*.md", "type": "daily_reflection", "label": "每日反思"},
            {"pattern": "*-reflection.md", "type": "structured_reflection", "label": "结构化反思"},
            {"pattern": "*小程序*.md", "type": "miniprogram_note", "label": "小程序"},
        ],
        "exclude_patterns": ["reflections.*", "deploy.*", "*temp*", "*草稿*"],
    },
    "exclude_dirs": ["miniprogram", "cloud-sync", "__pycache__", "kaiqi", "采购管理"],
    "output": {
        "remote_json": "reflections.json",
        "offline_js": "miniprogram/data/reflections.js",
        "offline_max_days": 30,
    },
    "type_order": [
        "daily_reflection",
        "structured_reflection",
        "miniprogram_note",
    ],
}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy via json
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8") as f:
            user = json.load(f)
        # shallow-merge top keys; nested dicts replaced if present
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict) and k in (
                "window",
                "inclusion",
                "output",
            ):
                merged = dict(cfg[k])
                merged.update(v)
                cfg[k] = merged
            else:
                cfg[k] = v
    return cfg


def extract_date_from_filename(filename: str) -> str | None:
    """Extract YYYY-MM-DD from leading date in filename."""
    match = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", filename)
    if not match:
        return None
    year, month, day = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def extract_title_from_filename(filename: str) -> str:
    """Strip date prefix and extension → readable title."""
    name = re.sub(r"\.md$", "", filename, flags=re.I)
    name = re.sub(r"^\d{4}-\d{1,2}-\d{1,2}-?", "", name)
    return name.strip() or filename


def extract_title(content: str) -> str | None:
    for line in content.split("\n"):
        if line.startswith("# "):
            t = line[2:].strip()
            # drop leading emoji-ish tokens lightly
            t = re.sub(r"^[🪞📝🔍📖\s]+", "", t).strip()
            if t:
                return t
    return None


def extract_summary(content: str) -> str:
    """Daily-reflection style summary (一句话总结) with fallbacks."""
    lines = content.split("\n")
    in_summary = False
    summary_lines = []
    for line in lines:
        if "一句话总结" in line and line.strip().startswith("##"):
            in_summary = True
            continue
        if in_summary:
            if line.startswith("## ") or line.startswith("---"):
                break
            if line.strip():
                summary_lines.append(line.strip())

    if summary_lines:
        text = " ".join(summary_lines)
        return text[:200] + ("..." if len(text) > 200 else "")

    for line in lines:
        if line.startswith("> ") and len(line) > 20:
            return line[2:].strip()[:150] + ("..." if len(line) > 152 else "")
    return generic_summary(content)


def extract_reflection_summary(content: str) -> str:
    lines = content.split("\n")
    summary_lines = []
    in_event = False
    for line in lines:
        if line.startswith("## 事件") or line.startswith("## 目标与结果"):
            in_event = True
            continue
        if in_event:
            if line.startswith("## ") or line.startswith("---"):
                break
            if line.strip() and not line.startswith("- ") and not line.startswith("#"):
                summary_lines.append(line.strip())
                if len(summary_lines) >= 2:
                    break

    if summary_lines:
        return " ".join(summary_lines)[:200]
    return generic_summary(content) or "反思记录"


def generic_summary(content: str, limit: int = 180) -> str:
    """First meaningful paragraph / line for arbitrary markdown."""
    lines = content.split("\n")
    buf = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("---") or s.startswith("```"):
            if buf:
                break
            continue
        if s.startswith(">"):
            s = s.lstrip("> ").strip()
        if s.startswith("- ") or s.startswith("* "):
            s = s[2:].strip()
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
        if len(s) < 4:
            continue
        buf.append(s)
        if sum(len(x) for x in buf) >= limit:
            break
    if not buf:
        return ""
    text = " ".join(buf)
    return text[:limit] + ("..." if len(text) > limit else "")


def extract_tags(content: str) -> list[str]:
    tags: list[str] = []
    tag_patterns = [
        r"薄弱点\s*\d*[：:]\s*(.+?)(?:\n|$)",
        r"###\s*(.+?)(?:\n|$)",
    ]
    for pattern in tag_patterns:
        for m in re.findall(pattern, content)[:2]:
            clean = m.strip().replace("**", "")[:30]
            if clean and clean not in tags:
                tags.append(clean)
    return tags[:3]


def match_pattern(filename: str, pattern: str) -> bool:
    return fnmatch.fnmatch(filename, pattern)


def classify_file(filename: str, inclusion: dict) -> dict | None:
    """Return first matching pattern meta, or None if excluded / no match."""
    name = filename
    for ex in inclusion.get("exclude_patterns") or []:
        if match_pattern(name, ex):
            return None
    for p in inclusion.get("patterns") or []:
        if match_pattern(name, p["pattern"]):
            return p
    return None


def discover_month_dirs(vault_root: Path, month_re: str, exclude_dirs: list[str]) -> list[tuple[int, int, str, Path]]:
    """Return sorted list of (year, month, dirname, path)."""
    rx = re.compile(month_re)
    found: list[tuple[int, int, str, Path]] = []
    if not vault_root.is_dir():
        return found
    exclude = set(exclude_dirs or [])
    for p in vault_root.iterdir():
        if not p.is_dir() or p.name in exclude or p.name.startswith("."):
            continue
        m = rx.match(p.name)
        if not m:
            continue
        # group from full match
        parts = re.match(r"^(\d{4})-(\d{1,2})$", p.name)
        if not parts:
            continue
        y, mo = int(parts.group(1)), int(parts.group(2))
        found.append((y, mo, p.name, p))
    found.sort(key=lambda x: (x[0], x[1]))
    return found


def rolling_window(
    months: list[tuple[int, int, str, Path]], n: int, now: datetime | None = None
) -> list[tuple[int, int, str, Path]]:
    """Take last n month dirs with (year,month) <= current month."""
    if n <= 0:
        return []
    now = now or datetime.now(CST)
    anchor = (now.year, now.month)
    eligible = [m for m in months if (m[0], m[1]) <= anchor]
    if not eligible:
        return []
    return eligible[-n:]


def build_doc(path: Path, meta: dict, type_counters: dict) -> dict | None:
    date_str = extract_date_from_filename(path.name)
    if not date_str:
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[warn] cannot read {path}: {e}")
        return None

    doc_type = meta["type"]
    label = meta.get("label") or doc_type
    title = extract_title(content) or extract_title_from_filename(path.name)

    if doc_type == "daily_reflection":
        summary = extract_summary(content) or ""
        tags = extract_tags(content)
    elif doc_type == "structured_reflection":
        summary = extract_reflection_summary(content)
        tags = extract_tags(content)
        if not title or title == extract_title_from_filename(path.name):
            title = "结构化反思"
    else:
        summary = generic_summary(content)
        tags = extract_tags(content)

    idx = type_counters.get(date_str + doc_type, 0)
    type_counters[date_str + doc_type] = idx + 1
    doc_id = f"{date_str}-{doc_type}-{idx}"

    return {
        "id": doc_id,
        "date": date_str,
        "type": doc_type,
        "label": label,
        "title": title or label,
        "summary": summary or "",
        "tags": tags,
        "filename": path.name,
        "content": content,
    }


def sort_docs(docs: list[dict], type_order: list[str]) -> list[dict]:
    order = {t: i for i, t in enumerate(type_order or [])}

    def key(d: dict):
        return (order.get(d["type"], 99), d.get("filename") or "")

    return sorted(docs, key=key)


def days_to_legacy_reflections(days: list[dict]) -> list[dict]:
    """v1-shaped list for HTML template + optional consumers."""
    legacy = []
    for day in days:
        docs = day.get("docs") or []
        if not docs:
            continue
        primary = docs[0]
        structured = next((d for d in docs if d["type"] == "structured_reflection"), None)
        daily = next((d for d in docs if d["type"] == "daily_reflection"), None)
        # content for HTML: join all docs with headers
        parts = []
        for d in docs:
            parts.append(f"## {d.get('label', '')} · {d.get('title', '')}\n\n{d.get('content', '')}")
        entry = {
            "date": day["date"],
            "type": "merged",
            "title": primary.get("title") or "",
            "summary": primary.get("summary") or "",
            "tags": primary.get("tags") or [],
            "content": "\n\n---\n\n".join(parts),
            "daily_content": (daily or primary).get("content") or "",
            "filename": primary.get("filename") or "",
            "has_reflection": structured is not None or len(docs) > 1,
            "reflection_title": (structured or {}).get("title") or "",
            "reflection_summary": (structured or {}).get("summary") or "",
            "reflection_tags": (structured or {}).get("tags") or [],
            "reflection_content": (structured or {}).get("content") or "",
            "docs": docs,
        }
        # v1 camelCase mirror fields for offline compat consumers
        entry["hasReflection"] = entry["has_reflection"]
        entry["reflectionSummary"] = entry["reflection_summary"]
        entry["reflectionTags"] = entry["reflection_tags"]
        entry["reflectionContent"] = entry["reflection_content"]
        legacy.append(entry)
    return legacy


def to_miniprogram_day(day: dict) -> dict:
    docs_out = []
    for d in day.get("docs") or []:
        docs_out.append(
            {
                "id": d["id"],
                "type": d["type"],
                "label": d.get("label") or "",
                "title": d.get("title") or "",
                "summary": d.get("summary") or "",
                "tags": d.get("tags") or [],
                "filename": d.get("filename") or "",
                "content": d.get("content") or "",
            }
        )
    return {"date": day["date"], "docs": docs_out, "docCount": len(docs_out)}


def to_v1_compat_entry(day: dict) -> dict:
    """Flatten first+structured for old clients that only read reflections[]."""
    docs = day.get("docs") or []
    primary = docs[0] if docs else {}
    structured = next((d for d in docs if d["type"] == "structured_reflection"), None)
    return {
        "date": day["date"],
        "summary": primary.get("summary") or "",
        "tags": primary.get("tags") or [],
        "content": primary.get("content") or "",
        "hasReflection": structured is not None,
        "reflectionSummary": (structured or {}).get("summary") or "",
        "reflectionTags": (structured or {}).get("tags") or [],
        "reflectionContent": (structured or {}).get("content") or "",
        "docs": [
            {
                "id": d["id"],
                "type": d["type"],
                "label": d.get("label") or "",
                "title": d.get("title") or "",
                "summary": d.get("summary") or "",
                "tags": d.get("tags") or [],
                "filename": d.get("filename") or "",
                "content": d.get("content") or "",
            }
            for d in docs
        ],
        "docCount": len(docs),
    }


def export_data_files(
    out_dir: Path,
    days: list[dict],
    window_months: list[str],
    cfg: dict,
) -> dict:
    """
    Export:
      1) reflections.json — schema v2 for remote
      2) miniprogram/data/reflections.js — offline fallback (recent N days)
    """
    mp_days = [to_miniprogram_day(d) for d in days]
    v1_list = [to_v1_compat_entry(d) for d in days]
    payload = {
        "schemaVersion": 2,
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {"months": window_months, "mode": "rolling", "size": cfg.get("window", {}).get("months", 2)},
        "count": len(mp_days),
        "docCount": sum(d["docCount"] for d in mp_days),
        "days": mp_days,
        "reflections": v1_list,  # compat + includes docs for smart clients
    }

    out_name = (cfg.get("output") or {}).get("remote_json") or "reflections.json"
    json_path = out_dir / out_name
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Generated {json_path} ({payload['count']} days, {payload['docCount']} docs)")

    offline_max = int((cfg.get("output") or {}).get("offline_max_days") or 30)
    offline_days = mp_days[-offline_max:] if offline_max > 0 else mp_days
    offline_payload = {
        "schemaVersion": 2,
        "updatedAt": payload["updatedAt"],
        "window": payload["window"],
        "count": len(offline_days),
        "docCount": sum(d["docCount"] for d in offline_days),
        "days": offline_days,
        "reflections": v1_list[-offline_max:] if offline_max > 0 else v1_list,
    }

    rel_js = (cfg.get("output") or {}).get("offline_js") or "miniprogram/data/reflections.js"
    js_path = out_dir / rel_js
    js_path.parent.mkdir(parents=True, exist_ok=True)
    js_body = (
        "// Auto-generated by build_reflection_app.py — do not edit by hand\n"
        "// Offline fallback (schema v2). Online data from config.dataUrl.\n"
        "module.exports = "
        + json.dumps(offline_payload, ensure_ascii=False, indent=2)
        + ";\n"
    )
    js_path.write_text(js_body, encoding="utf-8")
    print(f"Generated {js_path} (offline {len(offline_days)} days)")

    return payload


def scan_directory(dir_path: Path, inclusion: dict, seen_files: set[str]) -> list[dict]:
    """Scan one directory for matching md files → docs."""
    docs: list[dict] = []
    type_counters: dict[str, int] = {}
    if not dir_path.is_dir():
        return docs
    for f in sorted(dir_path.glob("*.md")):
        # de-dupe by filename across months (shouldn't collide)
        key = f.name
        if key in seen_files:
            continue
        meta = classify_file(f.name, inclusion)
        if not meta:
            continue
        if inclusion.get("require_date_prefix", True) and not extract_date_from_filename(f.name):
            continue
        doc = build_doc(f, meta, type_counters)
        if doc:
            seen_files.add(key)
            docs.append(doc)
            print(f"  + [{doc['type']}] {f.parent.name}/{f.name}")
    return docs


def collect_days(
    scan_dirs: list[Path],
    inclusion: dict,
    type_order: list[str],
) -> list[dict]:
    seen: set[str] = set()
    all_docs: list[dict] = []
    for d in scan_dirs:
        all_docs.extend(scan_directory(d, inclusion, seen))

    by_date: dict[str, list[dict]] = defaultdict(list)
    for doc in all_docs:
        by_date[doc["date"]].append(doc)

    days = []
    for date_str in sorted(by_date.keys()):
        docs = sort_docs(by_date[date_str], type_order)
        days.append({"date": date_str, "docs": docs})
    return days


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build reflections.json/html from -A Daily vault (rolling window)"
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="单目录扫描（云端 workflow：已拉好的 md 扁平/目录）。设置后跳过 vault 滚动窗口。",
    )
    parser.add_argument(
        "--vault-root",
        default=None,
        help="覆盖 feed.config.json 的 vault_root",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="产物输出目录，默认脚本所在目录",
    )
    args = parser.parse_args()

    cfg = load_config()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else SCRIPT_DIR
    inclusion = cfg.get("inclusion") or DEFAULT_CONFIG["inclusion"]
    type_order = cfg.get("type_order") or DEFAULT_CONFIG["type_order"]
    window_cfg = cfg.get("window") or {"mode": "rolling", "months": 2}
    window_n = int(window_cfg.get("months") or 2)

    window_labels: list[str] = []

    if args.workspace:
        # Cloud / explicit: scan given dir only (and one level of month subdirs if present)
        ws = Path(args.workspace).resolve()
        month_re = cfg.get("month_dir_regex") or r"^\d{4}-\d{1,2}$"
        sub_months = discover_month_dirs(ws, month_re, cfg.get("exclude_dirs") or [])
        if sub_months:
            scan_dirs = [m[3] for m in sub_months]
            window_labels = [m[2] for m in sub_months]
            print(f"Workspace mode (month subdirs): {window_labels}")
        else:
            scan_dirs = [ws]
            window_labels = [ws.name]
            print(f"Workspace mode (flat): {ws}")
    else:
        vault_root_cfg = args.vault_root or cfg.get("vault_root") or ".."
        vault_root = Path(vault_root_cfg)
        if not vault_root.is_absolute():
            vault_root = (SCRIPT_DIR / vault_root).resolve()
        else:
            vault_root = vault_root.resolve()

        months = discover_month_dirs(
            vault_root,
            cfg.get("month_dir_regex") or r"^\d{4}-\d{1,2}$",
            cfg.get("exclude_dirs") or [],
        )
        selected = rolling_window(months, window_n)
        scan_dirs = [m[3] for m in selected]
        window_labels = [m[2] for m in selected]
        print(f"Vault: {vault_root}")
        print(f"Rolling window ({window_n}): {window_labels}")

    if not scan_dirs:
        print("[warn] no month directories to scan")

    days = collect_days(scan_dirs, inclusion, type_order)
    print(f"Found {len(days)} days with documents")

    export_data_files(out_dir, days, window_labels, cfg)

    # Legacy list for HTML template (multi-doc concatenated content)
    reflections = days_to_legacy_reflections(days)
    data_json = json.dumps(reflections, ensure_ascii=False, indent=2)

    # --- HTML generation (Apple-style template, multi-doc joined per day) ---
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<title>每日反思 | Daily Reflection</title>
<style>
:root {{
  --bg-primary: #f2f2f7;
  --bg-secondary: #ffffff;
  --bg-tertiary: #f9f9fb;
  --bg-elevated: rgba(255,255,255,0.92);
  --text-primary: #1c1c1e;
  --text-secondary: #3c3c43;
  --text-tertiary: #8e8e93;
  --accent: #007aff;
  --accent-soft: rgba(0,122,255,0.12);
  --accent-hover: #0066d6;
  --separator: rgba(60,60,67,0.12);
  --shadow-sm: 0 1px 3px rgba(0,0,0,0.04);
  --shadow: 0 2px 12px rgba(0,0,0,0.06);
  --shadow-lg: 0 8px 40px rgba(0,0,0,0.1);
  --radius-lg: 20px;
  --radius: 14px;
  --radius-sm: 10px;
  --font: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'SF Pro Text', 'Helvetica Neue', 'PingFang SC', sans-serif;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg-primary: #000000;
    --bg-secondary: #1c1c1e;
    --bg-tertiary: #2c2c2e;
    --bg-elevated: rgba(28,28,30,0.92);
    --text-primary: #ffffff;
    --text-secondary: #ebf5ff;
    --text-tertiary: #8e8e93;
    --accent: #0a84ff;
    --accent-soft: rgba(10,132,255,0.18);
    --separator: rgba(84,84,88,0.36);
  }}
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: var(--font);
  background: var(--bg-primary);
  color: var(--text-primary);
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}}
.header {{
  position: fixed; top: 0; left: 0; right: 0; z-index: 100;
  background: var(--bg-elevated);
  backdrop-filter: saturate(180%) blur(24px);
  -webkit-backdrop-filter: saturate(180%) blur(24px);
  border-bottom: 1px solid var(--separator);
  padding: 12px 16px;
  display: flex; align-items: center; justify-content: space-between;
}}
.header button {{
  width: 36px; height: 36px; border: none; border-radius: 50%;
  background: var(--bg-tertiary); color: var(--accent); font-size: 20px; cursor: pointer;
}}
.header-center {{ text-align: center; flex: 1; cursor: pointer; }}
.header h1 {{ font-size: 17px; font-weight: 600; }}
.header .sub {{ font-size: 11px; color: var(--text-tertiary); }}
.content {{ max-width: 640px; margin: 0 auto; padding: 72px 16px 40px; }}
.stats {{ display: flex; gap: 8px; margin-bottom: 16px; }}
.stat {{ flex: 1; background: var(--bg-secondary); border-radius: var(--radius-sm); padding: 12px; text-align: center; box-shadow: var(--shadow-sm); }}
.stat .v {{ font-size: 20px; font-weight: 700; color: var(--accent); display: block; }}
.stat .l {{ font-size: 11px; color: var(--text-tertiary); }}
.hero {{ display: flex; align-items: baseline; gap: 12px; margin: 16px 0; }}
.hero .day {{ font-size: 56px; font-weight: 700; letter-spacing: -2px; line-height: 1; }}
.hero .meta {{ color: var(--text-secondary); font-size: 14px; }}
.badge {{ display: inline-block; background: var(--accent-soft); color: var(--accent); font-size: 11px; padding: 2px 8px; border-radius: 8px; margin-left: 6px; }}
.card {{
  background: var(--bg-secondary); border-radius: var(--radius); padding: 16px 18px;
  margin-bottom: 12px; box-shadow: var(--shadow);
}}
.card-label {{ font-size: 12px; font-weight: 600; color: var(--accent); margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px; }}
.card-title {{ font-size: 16px; font-weight: 600; margin-bottom: 6px; }}
.summary {{ font-size: 15px; line-height: 1.55; color: var(--text-secondary); white-space: pre-wrap; }}
.tags {{ margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px; }}
.tag {{ font-size: 11px; background: var(--bg-tertiary); color: var(--text-secondary); padding: 3px 8px; border-radius: 6px; }}
.detail {{ margin-top: 12px; font-size: 14px; line-height: 1.65; white-space: pre-wrap; color: var(--text-primary); border-top: 1px solid var(--separator); padding-top: 12px; }}
.toggle {{
  display: block; width: 100%; margin-top: 8px; padding: 12px;
  border: none; border-radius: var(--radius-sm); background: var(--accent); color: #fff;
  font-size: 15px; font-weight: 600; cursor: pointer;
}}
.empty {{ text-align: center; padding: 48px 16px; color: var(--text-tertiary); }}
.cal-overlay {{
  display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 200;
}}
.cal-overlay.open {{ display: block; }}
.cal {{
  display: none; position: fixed; left: 0; right: 0; bottom: 0; z-index: 201;
  background: var(--bg-secondary); border-radius: 20px 20px 0 0; padding: 16px 16px 32px;
  max-height: 70vh; overflow: auto;
}}
.cal.open {{ display: block; }}
.cal-nav {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }}
.cal-nav button {{ border: none; background: var(--bg-tertiary); width: 32px; height: 32px; border-radius: 50%; cursor: pointer; color: var(--accent); font-size: 18px; }}
.cal-grid {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; text-align: center; }}
.cal-cell {{ padding: 10px 0; border-radius: 10px; font-size: 14px; cursor: pointer; }}
.cal-cell.has {{ font-weight: 700; color: var(--accent); }}
.cal-cell.active {{ background: var(--accent); color: #fff; }}
.cal-cell.other {{ opacity: 0.3; }}
.weekdays {{ display: grid; grid-template-columns: repeat(7, 1fr); text-align: center; font-size: 11px; color: var(--text-tertiary); margin-bottom: 6px; }}
</style>
</head>
<body>
  <div class="header">
    <button type="button" id="prevBtn" aria-label="prev">‹</button>
    <div class="header-center" id="openCal">
      <h1 id="headerTitle">每日反思</h1>
      <div class="sub" id="headerSub">点击选择日期</div>
    </div>
    <button type="button" id="nextBtn" aria-label="next">›</button>
  </div>
  <div class="content">
    <div class="stats">
      <div class="stat"><span class="v" id="statDays">--</span><span class="l">有记录天数</span></div>
      <div class="stat"><span class="v" id="statDocs">--</span><span class="l">文档篇数</span></div>
      <div class="stat"><span class="v" id="statIdx">--</span><span class="l">当前序号</span></div>
    </div>
    <div class="hero">
      <div class="day" id="dayNum">--</div>
      <div class="meta">
        <div id="monthYear"></div>
        <div id="weekday"></div>
        <span class="badge" id="docBadge" style="display:none"></span>
      </div>
    </div>
    <div id="docList"></div>
    <div class="empty" id="empty" style="display:none">这一天没有记录</div>
  </div>
  <div class="cal-overlay" id="calOverlay"></div>
  <div class="cal" id="cal">
    <div class="cal-nav">
      <button type="button" id="calPrev">‹</button>
      <strong id="calLabel"></strong>
      <button type="button" id="calNext">›</button>
    </div>
    <div class="weekdays"><span>日</span><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span></div>
    <div class="cal-grid" id="calGrid"></div>
  </div>
<script>
const reflections = {data_json};
const dateMap = new Map();
reflections.forEach(r => dateMap.set(r.date, r));
const weekdays = ['周日','周一','周二','周三','周四','周五','周六'];
const monthNames = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'];
let currentDate = reflections.length ? reflections[reflections.length - 1].date : null;
let calY = currentDate ? +currentDate.slice(0,4) : new Date().getFullYear();
let calM = currentDate ? +currentDate.slice(5,7) : new Date().getMonth()+1;
const openDetail = new Set();

function updateDisplay() {{
  if (!currentDate) return;
  const r = dateMap.get(currentDate);
  const [y,m,d] = currentDate.split('-').map(Number);
  const dt = new Date(y, m-1, d);
  document.getElementById('headerTitle').textContent =
    `${{y}}.${{String(m).padStart(2,'0')}}.${{String(d).padStart(2,'0')}}`;
  document.getElementById('headerSub').textContent = weekdays[dt.getDay()] + ' · 点击选择日期';
  document.getElementById('dayNum').textContent = d;
  document.getElementById('monthYear').textContent = `${{y}}年${{monthNames[m-1]}}`;
  document.getElementById('weekday').textContent = weekdays[dt.getDay()];
  const idx = reflections.findIndex(x => x.date === currentDate);
  document.getElementById('statDays').textContent = reflections.length;
  document.getElementById('statDocs').textContent = reflections.reduce((n,x) => n + ((x.docs&&x.docs.length)||1), 0);
  document.getElementById('statIdx').textContent = idx >= 0 ? '#' + (idx+1) : '--';
  const list = document.getElementById('docList');
  const empty = document.getElementById('empty');
  list.innerHTML = '';
  if (!r) {{
    empty.style.display = 'block';
    document.getElementById('docBadge').style.display = 'none';
    return;
  }}
  empty.style.display = 'none';
  const docs = (r.docs && r.docs.length) ? r.docs : [{{
    id: r.date + '-primary', label: '记录', title: r.title || '', summary: r.summary || '',
    tags: r.tags || [], content: r.content || ''
  }}];
  const badge = document.getElementById('docBadge');
  badge.style.display = 'inline-block';
  badge.textContent = docs.length + ' 篇';
  docs.forEach((doc, i) => {{
    const id = doc.id || (currentDate + '-' + i);
    const card = document.createElement('div');
    card.className = 'card';
    const tags = (doc.tags || []).map(t => `<span class="tag">${{escapeHtml(t)}}</span>`).join('');
    const open = openDetail.has(id);
    card.innerHTML = `
      <div class="card-label">${{escapeHtml(doc.label || doc.type || '文档')}}</div>
      <div class="card-title">${{escapeHtml(doc.title || '')}}</div>
      <div class="summary">${{escapeHtml(doc.summary || '')}}</div>
      ${{tags ? `<div class="tags">${{tags}}</div>` : ''}}
      ${{open ? `<div class="detail">${{escapeHtml(doc.content || '')}}</div>` : ''}}
      <button type="button" class="toggle" data-id="${{id}}">${{open ? '收起' : '查看全文'}}</button>
    `;
    list.appendChild(card);
  }});
  list.querySelectorAll('.toggle').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const id = btn.getAttribute('data-id');
      if (openDetail.has(id)) openDetail.delete(id); else openDetail.add(id);
      updateDisplay();
    }});
  }});
  document.getElementById('prevBtn').disabled = idx <= 0;
  document.getElementById('nextBtn').disabled = idx >= reflections.length - 1;
}}

function escapeHtml(s) {{
  return String(s || '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

function buildCal() {{
  document.getElementById('calLabel').textContent = calY + '年' + calM + '月';
  const grid = document.getElementById('calGrid');
  grid.innerHTML = '';
  const first = new Date(calY, calM-1, 1).getDay();
  const dim = new Date(calY, calM, 0).getDate();
  for (let i=0;i<first;i++) {{
    const c = document.createElement('div'); c.className='cal-cell other'; grid.appendChild(c);
  }}
  for (let d=1; d<=dim; d++) {{
    const ds = `${{calY}}-${{String(calM).padStart(2,'0')}}-${{String(d).padStart(2,'0')}}`;
    const c = document.createElement('div');
    c.className = 'cal-cell' + (dateMap.has(ds)?' has':'') + (ds===currentDate?' active':'');
    c.textContent = d;
    if (dateMap.has(ds)) c.onclick = () => {{ currentDate = ds; closeCal(); updateDisplay(); }};
    grid.appendChild(c);
  }}
}}
function openCal() {{ document.getElementById('cal').classList.add('open'); document.getElementById('calOverlay').classList.add('open'); buildCal(); }}
function closeCal() {{ document.getElementById('cal').classList.remove('open'); document.getElementById('calOverlay').classList.remove('open'); }}

document.getElementById('prevBtn').onclick = () => {{
  const i = reflections.findIndex(x => x.date === currentDate);
  if (i > 0) {{ currentDate = reflections[i-1].date; openDetail.clear(); updateDisplay(); }}
}};
document.getElementById('nextBtn').onclick = () => {{
  const i = reflections.findIndex(x => x.date === currentDate);
  if (i >= 0 && i < reflections.length-1) {{ currentDate = reflections[i+1].date; openDetail.clear(); updateDisplay(); }}
}};
document.getElementById('openCal').onclick = openCal;
document.getElementById('calOverlay').onclick = closeCal;
document.getElementById('calPrev').onclick = () => {{ calM--; if (calM<1){{calM=12;calY--;}} buildCal(); }};
document.getElementById('calNext').onclick = () => {{ calM++; if (calM>12){{calM=1;calY++;}} buildCal(); }};
updateDisplay();
</script>
</body>
</html>'''

    output_path = out_dir / "reflections.html"
    output_path.write_text(html, encoding="utf-8")
    print(f"Generated {output_path}")
    print(f"Total days: {len(days)}")
    for day in days:
        n = len(day.get("docs") or [])
        names = ", ".join(d.get("filename") or "?" for d in day.get("docs") or [])
        print(f"  {day['date']} ({n}): {names}")


if __name__ == "__main__":
    main()
