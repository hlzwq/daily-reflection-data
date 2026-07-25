#!/usr/bin/env python3
"""
从 TeraCloud (InfiniCloud) WebDAV 拉取当月反思 md 到本地目录。

供 GitHub Actions 调用：拉完后由 build_reflection_app.py 生成 reflections.json。
全程纯标准库，无需 pip install。

环境变量:
  WEBDAV_BASE      WebDAV 根，如 https://higa.teracloud.jp/dav/
  WEBDAV_USER      用户名
  WEBDAV_PASSWORD  密码（应用密码）

用法:
  python fetch_webdav.py --out sources
  python fetch_webdav.py --out sources --month-dir "-A Daily/2026-7"   # 覆盖默认当月
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))


def current_month_dir() -> str:
    """当月目录名，如 -A Daily/2026-7（月份不补零，与 Obsidian 目录规范一致）。"""
    now = datetime.now(CST)
    return f"-A Daily/{now.year}-{now.month}"


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
            continue  # 目录自身
        hrefs.append(raw)
    return hrefs


def get_file(base: str, href: str, headers: dict) -> bytes:
    if href.startswith("http://") or href.startswith("https://"):
        url = href
    else:
        url = origin_of(base) + href  # href 形如 /dav/-A%20Daily/.../x.md
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GET {href} 失败: HTTP {e.code}") from e


def probe_list(base: str, headers: dict, dir_path: str, depth: int, max_depth: int) -> None:
    """递归列出 WebDAV 目录树（诊断用）：路径 404 时定位 -A Daily 的真实位置。"""
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
        url, data=body.encode("utf-8"), method="PROPFIND",
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


def main() -> None:
    parser = argparse.ArgumentParser(description="从 WebDAV 拉取当月反思 md")
    parser.add_argument("--out", required=True, help="本地输出目录")
    parser.add_argument(
        "--month-dir",
        default=None,
        help='月份子目录，如 "-A Daily/2026-7"，默认当月',
    )
    args = parser.parse_args()

    base = os.environ.get("WEBDAV_BASE", "").strip()
    user = os.environ.get("WEBDAV_USER", "").strip()
    pwd = os.environ.get("WEBDAV_PASSWORD", "").strip()
    if not (base and user and pwd):
        print("[error] 需设置环境变量 WEBDAV_BASE / WEBDAV_USER / WEBDAV_PASSWORD")
        sys.exit(1)

    month_dir = args.month_dir or current_month_dir()
    print(f"=== WebDAV 拉取: {month_dir} ===")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    headers = auth_header(user, pwd)
    try:
        hrefs = propfind(base, month_dir, headers)
    except RuntimeError as e:
        print(f"[error] {e}")
        print(f"[info] WEBDAV_BASE={base}")
        print("[probe] 列出 WebDAV 根目录树（深度2）以定位 -A Daily 的实际路径：")
        probe_list(base, headers, "", 0, 2)
        sys.exit(1)
    md_count = 0
    for raw_href in hrefs:
        name = urllib.parse.unquote(raw_href.rstrip("/").split("/")[-1])
        if not name.lower().endswith(".md"):
            continue
        data = get_file(base, raw_href, headers)
        (out_dir / name).write_bytes(data)
        md_count += 1
        print(f"  fetched {name} ({len(data)} bytes)")

    if md_count == 0:
        print("[warn] 当月目录没有 .md（月初可能正常）")
    else:
        print(f"[ok] 共 {md_count} 个 md -> {out_dir}")


if __name__ == "__main__":
    main()
