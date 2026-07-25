#!/usr/bin/env python3
"""
把构建产物上传到阿里云 OSS（小程序的国内数据源）。

凭据从环境变量读：OSS_KEY_ID / OSS_KEY_SECRET。
webfox bucket 为公共读，对象上传后即可被小程序匿名 GET（无需签名）。

供 GitHub Actions 调用：在 build 生成 reflections.json / index.html 之后运行。

依赖:  pip install oss2

用法:
  python upload_oss.py \
    --endpoint https://oss-cn-hangzhou.aliyuncs.com \
    --bucket webfox \
    reflections.json index.html
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import oss2

# 按扩展名给一个规范 Content-Type；小程序按字符串解析，不强制，但规范点更好
CONTENT_TYPES = {
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="上传产物到阿里云 OSS")
    parser.add_argument("--endpoint", required=True, help="如 https://oss-cn-hangzhou.aliyuncs.com")
    parser.add_argument("--bucket", required=True, help="bucket 名，如 webfox")
    parser.add_argument("files", nargs="+", help="要上传的本地文件（对象名取文件名）")
    args = parser.parse_args()

    key_id = os.environ.get("OSS_KEY_ID", "").strip()
    key_secret = os.environ.get("OSS_KEY_SECRET", "").strip()
    if not key_id or not key_secret:
        print("[error] 需设置环境变量 OSS_KEY_ID / OSS_KEY_SECRET")
        sys.exit(1)

    auth = oss2.Auth(key_id, key_secret)
    bucket = oss2.Bucket(auth, args.endpoint, args.bucket)

    host = args.endpoint.replace("https://", "").replace("http://", "")
    for local in args.files:
        path = Path(local)
        if not path.is_file():
            print(f"[warn] 跳过不存在的文件: {local}")
            continue
        key = path.name
        ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        bucket.put_object_from_file(key, local, headers={"Content-Type": ctype})
        url = f"https://{args.bucket}.{host}/{key}"
        print(f"  uploaded {key} ({path.stat().st_size} bytes) -> {url}")

    print(f"[ok] 共 {len(args.files)} 个文件已同步到 oss://{args.bucket}")


if __name__ == "__main__":
    main()
