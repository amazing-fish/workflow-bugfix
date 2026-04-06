# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
from typing import Any


def _read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _normalize_curl_text(text: str) -> str:
    # 兼容多行反斜杠续行
    return text.replace("\\\n", " ").replace("\\\r\n", " ").strip()


def _extract_headers_from_curl(curl_text: str) -> dict[str, str]:
    text = _normalize_curl_text(curl_text)
    tokens = shlex.split(text)

    headers: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        is_header_flag = t in {"-H", "--header"}
        if is_header_flag and i + 1 < len(tokens):
            raw = tokens[i + 1]
            i += 2
        elif t.startswith("-H") and len(t) > 2:
            raw = t[2:]
            i += 1
        elif t.startswith("--header="):
            raw = t.split("=", 1)[1]
            i += 1
        else:
            i += 1
            continue

        if ":" not in raw:
            continue
        k, v = raw.split(":", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            continue

        # 过滤 HTTP/2 伪头与无意义项
        if k.startswith(":"):
            continue
        if k.lower() in {"content-length", "host"}:
            # 由 requests 自动计算/管理，保留可能引入冲突
            continue

        headers[k] = v

    return headers


def _merge_common(menu_h: dict[str, str], obs_h: dict[str, str], dl_h: dict[str, str]) -> dict[str, str]:
    common: dict[str, str] = {}
    all_keys = set(menu_h) | set(obs_h) | set(dl_h)
    for k in sorted(all_keys):
        vals = [x.get(k) for x in (menu_h, obs_h, dl_h)]
        present = [x for x in vals if x is not None]
        if len(present) == 3 and present[0] == present[1] == present[2]:
            common[k] = present[0]
    return common


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description="将 3 份 F12 cURL 转换为 bag_probe 可用 headers JSON")
    p.add_argument("--menu-curl-file", required=True, help="downloadMenu 的 cURL 文本文件")
    p.add_argument("--getobsid-curl-file", required=True, help="getObsId 的 cURL 文本文件")
    p.add_argument("--download-curl-file", required=True, help="download 的 cURL 文本文件")
    p.add_argument("--out-dir", default=".", help="输出目录，默认当前目录")
    args = p.parse_args()

    menu_h = _extract_headers_from_curl(_read_text(args.menu_curl_file))
    obs_h = _extract_headers_from_curl(_read_text(args.getobsid_curl_file))
    dl_h = _extract_headers_from_curl(_read_text(args.download_curl_file))
    common_h = _merge_common(menu_h, obs_h, dl_h)

    out_dir = Path(args.out_dir)
    menu_path = out_dir / "menu_headers.json"
    obs_path = out_dir / "getobsid_headers.json"
    dl_path = out_dir / "download_headers.json"
    common_path = out_dir / "common_headers.json"

    _write_json(menu_path, menu_h)
    _write_json(obs_path, obs_h)
    _write_json(dl_path, dl_h)
    _write_json(common_path, common_h)

    print("[INFO] 已生成 headers 文件:")
    print(f"  - {menu_path}")
    print(f"  - {obs_path}")
    print(f"  - {dl_path}")
    print(f"  - {common_path}")

    print("\n[INFO] bag_probe 使用示例:")
    print(
        "python bag_probe.py --config config.json --bucket <bucket> --remote-path <path> --user-name <user> "
        f"--menu-headers-file {menu_path} --getobsid-headers-file {obs_path} "
        f"--download-headers-file {dl_path} --exact-headers"
    )


if __name__ == "__main__":
    main()
