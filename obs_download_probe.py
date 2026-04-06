# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import json
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _merge_headers(base: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = v
    return out


def _validate_probe_config(cfg: dict) -> None:
    required = ["get_obs_id_url", "download_base_url", "request_body"]
    for key in required:
        if not cfg.get(key):
            raise ValueError(f"obs_download_probe 缺少必要字段: {key}")

    body = cfg["request_body"]
    for key in ["files", "bucket", "userName"]:
        if key not in body:
            raise ValueError(f"obs_download_probe.request_body 缺少必要字段: {key}")
    if not body["files"]:
        raise ValueError("obs_download_probe.request_body.files 不能为空")


def _looks_like_zip(content: bytes) -> bool:
    return len(content) >= 4 and content[:4] == b"PK\x03\x04"


def _validate_bag_magic(content: bytes, source: str) -> None:
    if content.startswith(b"#ROSBAG"):
        return
    prefix = content[:64]
    preview = prefix.decode("utf-8", errors="replace").replace("\n", "\\n").replace("\r", "\\r")
    raise RuntimeError(
        f"bag 文件头校验失败: source={source}, size={len(content)}, "
        f"prefix_hex={prefix.hex()}, prefix_text={preview}"
    )


def _extract_filename(request_body: dict) -> str:
    first = request_body.get("files", [{}])[0]
    name = str(first.get("name") or "").strip()
    if name:
        return name
    file_path = str(first.get("path") or "").strip()
    if file_path:
        return Path(file_path).name
    return "downloaded.bag"


def _extract_obs_id_from_response(data) -> str | None:
    def pick_text(v):
        if v is None or isinstance(v, (dict, list)):
            return None
        text = str(v).strip()
        return text or None

    def walk(node):
        if isinstance(node, dict):
            for key in ("result", "opid", "obsId", "obs_id", "operationId"):
                if key not in node:
                    continue
                value = node.get(key)
                if isinstance(value, (dict, list)):
                    nested = walk(value)
                    if nested:
                        return nested
                else:
                    maybe = pick_text(value)
                    if maybe:
                        return maybe

            for key in ("url", "downloadUrl", "download_url"):
                raw_url = pick_text(node.get(key))
                if not raw_url:
                    continue
                m = re.search(r"[?&]opid=([^&]+)", raw_url)
                if m:
                    return m.group(1)

            for v in node.values():
                nested = walk(v)
                if nested:
                    return nested
            return None
        if isinstance(node, list):
            for item in node:
                nested = walk(item)
                if nested:
                    return nested
            return None
        return None

    return walk(data)


def run_probe(config_path: str | Path) -> dict:
    config_path = Path(config_path)
    cfg_all = json.loads(config_path.read_text(encoding="utf-8"))
    probe_cfg = dict(cfg_all.get("obs_download_probe") or {})
    if not probe_cfg:
        raise ValueError("config.json 缺少 obs_download_probe 配置")
    _validate_probe_config(probe_cfg)

    verify_ssl = bool(probe_cfg.get("verify_ssl", False))
    timeout_sec = int(probe_cfg.get("timeout_sec", 60))
    output_dir = Path(str(probe_cfg.get("output_dir") or "outputs/obs_probe"))
    output_dir.mkdir(parents=True, exist_ok=True)

    browser_headers = dict(cfg_all.get("browser_headers") or {})
    get_obs_id_headers = _merge_headers(browser_headers, dict(probe_cfg.get("get_obs_id_headers") or {}))
    download_headers = _merge_headers(browser_headers, dict(probe_cfg.get("download_headers") or {}))

    # 与浏览器一致：download 接口不带 content-type
    download_headers.pop("Content-Type", None)
    download_headers.pop("content-type", None)

    body = probe_cfg["request_body"]
    get_obs_id_url = str(probe_cfg["get_obs_id_url"])
    download_base_url = str(probe_cfg["download_base_url"]).rstrip("/")

    session = requests.Session()
    resp = session.post(
        get_obs_id_url,
        json=body,
        headers=get_obs_id_headers,
        verify=verify_ssl,
        timeout=timeout_sec,
    )
    resp.raise_for_status()
    data = resp.json()
    # 强制从 getObsId 响应动态提取，避免手工拷贝 opid
    obs_id = _extract_obs_id_from_response(data)
    if not obs_id:
        raise RuntimeError(f"getObsId 未返回 result: {data}")

    download_url = f"{download_base_url}/obs/v1/files/download?opid={obs_id}"
    dl_resp = session.get(download_url, headers=download_headers, verify=verify_ssl, timeout=timeout_sec)
    dl_resp.raise_for_status()
    content = dl_resp.content

    file_name = _extract_filename(body)
    if not file_name.endswith(".bag"):
        file_name += ".bag"
    target = output_dir / file_name

    if _looks_like_zip(content):
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            bag_members = [n for n in zf.namelist() if n.endswith(".bag")]
            if not bag_members:
                raise RuntimeError(f"download 返回 zip 但无 .bag 文件: members={zf.namelist()}")
            bag_data = zf.read(bag_members[0])
            _validate_bag_magic(bag_data, source=f"zip_member:{bag_members[0]}")
            target.write_bytes(bag_data)
            source_kind = "zip"
    else:
        _validate_bag_magic(content, source="raw_response")
        target.write_bytes(content)
        source_kind = "raw"

    result = {
        "status": "ok",
        "obs_id": str(obs_id),
        "obs_id_source": "getObsId_response",
        "download_url_host": urlparse(download_url).netloc,
        "saved_file": str(target),
        "saved_size": target.stat().st_size,
        "source_kind": source_kind,
    }
    result_path = output_dir / "probe_result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法: python obs_download_probe.py config.json")
        sys.exit(1)

    output = run_probe(sys.argv[1])
    print(json.dumps(output, ensure_ascii=False, indent=2))
