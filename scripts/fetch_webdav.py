#!/usr/bin/env python3
"""
从 TeraCloud (InfiniCloud) WebDAV 拉取滚动窗口内的反思 md 到本地目录。

供 GitHub Actions 调用：拉完后由 build_reflection_app.py 生成 reflections.json。
全程纯标准库，无需 pip install。

默认：滚动 2 个月（当前月 + 上一存在月目录），与 feed.config.json 对齐。

环境变量:
  WEBDAV_BASE      WebDAV 根，如 https://higa.teracloud.jp/dav/
  WEBDAV_USER      用户名
  WEBDAV_PASSWORD  密码（应用密码）
  WEBDAV_VAULT_PREFIX  vault 前缀，默认 action
  FEED_WINDOW_MONTHS   滚动月数，默认 2

用法:
  python fetch_webdav.py --out sources
  python fetch_webdav.py --out sources --months 2
  python fetch_webdav.py --out sources --month-dir "-A Daily/2026-7"   # 仅单月（调试）
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
MONTH_DIR_RE = re.compile(r"^(\d{4})-(\d{1,2})$")


def vault_base_path() -> str:
    """如 action/-A Daily。
    注意：GitHub Actions 里未配置的 secret 常以空字符串注入，
    不能用 getenv 默认值（空串会盖掉 default）。"""
    raw = os.environ.get("WEBDAV_VAULT_PREFIX")
    if raw is None or not str(raw).strip():
        prefix = "action"
    else:
        prefix = str(raw).strip().strip("/")
    base = (prefix + "/") if prefix else ""
    return f"{base}-A Daily"


def current_month_dir() -> str:
    """当月目录名，如 action/-A Daily/2026-7（月份不补零）。"""
    now = datetime.now(CST)
    return f"{vault_base_path()}/{now.year}-{now.month}"


def auth_header(user: str, pwd: str) -> dict:
    token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def origin_of(base: str) -> str:
    p = urllib.parse.urlparse(base)
    return f"{p.scheme}://{p.netloc}"


def propfind(base: str, dir_path: str, headers: dict) -> list[str]:
    """PROPFIND 目录（Depth:1），返回子项原始 href（URL 编码的绝对路径）。"""
    safe_dir = dir_path.strip("/")
    url = base.rstrip("/") + "/" + "/".join(
        urllib.parse.quote(seg, safe="") for seg in safe_dir.split("/")
    ) + "/"
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:">'
        '<d:prop><d:resourcetype/></d:prop>'
        '</d:propfind>'
    )
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        method="PROPFIND",
        headers={**headers, "Depth": "1", "Content-Type": "application/xml"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            xml_data = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode("utf-8", "replace")
        raise RuntimeError(f"PROPFIND {dir_path} 失败: HTTP {e.code} {detail}") from e
    return parse_hrefs(xml_data, exclude_last_segment=safe_dir.split("/")[-1])


def parse_hrefs(xml_data: str, exclude_last_segment: str) -> list[str]:
    hrefs: list[str] = []
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as e:
        raise RuntimeError(f"PROPFIND XML 解析失败: {e}") from e
    for resp in root:
        el = resp.find("{DAV:}href")
        if el is None or not el.text:
            continue
        raw = el.text.strip()
        name = urllib.parse.unquote(raw).rstrip("/").split("/")[-1]
        if not name or name == exclude_last_segment:
            continue
        hrefs.append(raw)
    return hrefs


def get_file(base: str, href: str, headers: dict) -> bytes:
    if href.startswith("http://") or href.startswith("https://"):
        url = href
    else:
        url = origin_of(base) + href
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GET {href} 失败: HTTP {e.code}") from e


def list_month_dirs(base: str, headers: dict) -> list[tuple[int, int, str]]:
    """列出 vault 下的 YYYY-M 子目录，返回 (year, month, dirname) 升序。"""
    vault = vault_base_path()
    hrefs = propfind(base, vault, headers)
    found: list[tuple[int, int, str]] = []
    for raw in hrefs:
        name = urllib.parse.unquote(raw.rstrip("/").split("/")[-1])
        m = MONTH_DIR_RE.match(name)
        if not m:
            continue
        found.append((int(m.group(1)), int(m.group(2)), name))
    found.sort(key=lambda x: (x[0], x[1]))
    return found


def rolling_month_dirs(all_months: list[tuple[int, int, str]], n: int) -> list[str]:
    now = datetime.now(CST)
    anchor = (now.year, now.month)
    eligible = [m for m in all_months if (m[0], m[1]) <= anchor]
    if not eligible:
        # fallback current name even if not listed
        return [f"{now.year}-{now.month}"]
    selected = eligible[-n:] if n > 0 else eligible
    return [m[2] for m in selected]


def probe_list(base: str, headers: dict, dir_path: str, depth: int, max_depth: int) -> None:
    indent = "  " * depth
    safe_dir = dir_path.strip("/")
    if safe_dir:
        url = base.rstrip("/") + "/" + "/".join(
            urllib.parse.quote(s, safe="") for s in safe_dir.split("/")
        ) + "/"
    else:
        url = base.rstrip("/") + "/"
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
    )
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        method="PROPFIND",
        headers={**headers, "Depth": "1", "Content-Type": "application/xml"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            xml_data = resp.read().decode("utf-8")
    except Exception as e:
        print(f"{indent}[列 {dir_path or '/'} 失败: {e}]")
        return
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        print(f"{indent}[{dir_path or '/'} XML 解析失败]")
        return
    self_last = safe_dir.split("/")[-1] if safe_dir else ""
    found_any = False
    for resp_el in root:
        href_el = resp_el.find("{DAV:}href")
        if href_el is None or not href_el.text:
            continue
        raw = href_el.text.strip()
        name = urllib.parse.unquote(raw.rstrip("/").split("/")[-1])
        if not name or name == self_last:
            continue
        found_any = True
        is_dir = resp_el.find(".//{DAV:}collection") is not None
        print(f"{indent}- {name}{'/' if is_dir else ''}")
        if is_dir and depth < max_depth:
            child = (dir_path + "/" + name).strip("/")
            probe_list(base, headers, child, depth + 1, max_depth)
    if not found_any:
        print(f"{indent}(空)")


def fetch_month(base: str, headers: dict, month_rel: str, out_subdir: Path) -> int:
    """Pull all .md from one month dir into out_subdir. Returns count."""
    out_subdir.mkdir(parents=True, exist_ok=True)
    try:
        hrefs = propfind(base, month_rel, headers)
    except RuntimeError as e:
        print(f"[warn] {month_rel}: {e}")
        return 0
    md_count = 0
    for raw_href in hrefs:
        name = urllib.parse.unquote(raw_href.rstrip("/").split("/")[-1])
        if not name.lower().endswith(".md"):
            continue
        data = get_file(base, raw_href, headers)
        (out_subdir / name).write_bytes(data)
        md_count += 1
        print(f"  fetched {month_rel}/{name} ({len(data)} bytes)")
    return md_count


def main() -> None:
    parser = argparse.ArgumentParser(description="从 WebDAV 拉取滚动窗口反思 md")
    parser.add_argument("--out", required=True, help="本地输出目录")
    parser.add_argument(
        "--month-dir",
        default=None,
        help='仅拉单月，如 "action/-A Daily/2026-7" 或 "-A Daily/2026-7"',
    )
    parser.add_argument(
        "--months",
        type=int,
        default=None,
        help="滚动月数（默认环境变量 FEED_WINDOW_MONTHS 或 2）",
    )
    args = parser.parse_args()

    base = os.environ.get("WEBDAV_BASE", "").strip()
    user = os.environ.get("WEBDAV_USER", "").strip()
    pwd = os.environ.get("WEBDAV_PASSWORD", "").strip()
    if not (base and user and pwd):
        print("[error] 需设置环境变量 WEBDAV_BASE / WEBDAV_USER / WEBDAV_PASSWORD")
        sys.exit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    headers = auth_header(user, pwd)
    vault = vault_base_path()

    if args.month_dir:
        month_rel = args.month_dir.strip("/")
        # allow short form "-A Daily/2026-7"
        if not month_rel.startswith(vault) and "Daily" in month_rel:
            if not month_rel.startswith("action"):
                # prepend prefix if user passed -A Daily/...
                prefix = os.environ.get("WEBDAV_VAULT_PREFIX", "action").strip("/")
                if not month_rel.startswith("-A"):
                    pass
                elif prefix and not month_rel.startswith(prefix):
                    month_rel = f"{prefix}/{month_rel}" if not month_rel.startswith(prefix) else month_rel
        # extract dirname for subfolder
        dirname = month_rel.rstrip("/").split("/")[-1]
        print(f"=== WebDAV 单月拉取: {month_rel} ===")
        try:
            n = fetch_month(base, headers, month_rel, out_dir / dirname)
        except RuntimeError as e:
            print(f"[error] {e}")
            print(f"[info] WEBDAV_BASE={base}")
            print("[probe] 列出 WebDAV 根目录树（深度2）：")
            probe_list(base, headers, "", 0, 2)
            sys.exit(1)
        print(f"[ok] {n} 个 md -> {out_dir / dirname}")
        return

    n_months = args.months
    if n_months is None:
        raw_m = os.environ.get("FEED_WINDOW_MONTHS")
        if raw_m is None or not str(raw_m).strip():
            n_months = 2
        else:
            n_months = int(str(raw_m).strip())

    print(f"=== WebDAV 滚动 {n_months} 月拉取 (vault={vault}) ===")
    try:
        all_months = list_month_dirs(base, headers)
    except RuntimeError as e:
        print(f"[error] 列出月份失败: {e}")
        print(f"[info] WEBDAV_BASE={base}")
        print("[probe] 列出 WebDAV 根目录树（深度2）：")
        probe_list(base, headers, "", 0, 2)
        sys.exit(1)

    selected = rolling_month_dirs(all_months, n_months)
    print(f"月份目录: {selected}")

    total = 0
    for dirname in selected:
        month_rel = f"{vault}/{dirname}"
        total += fetch_month(base, headers, month_rel, out_dir / dirname)

    if total == 0:
        print("[warn] 窗口内没有 .md（月初或路径不对）")
    else:
        print(f"[ok] 共 {total} 个 md -> {out_dir} (子目录: {selected})")


if __name__ == "__main__":
    main()
