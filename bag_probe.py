# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import io
import json
import time
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@dataclass
class ProbeResult:
    name: str
    ok: bool
    detail: dict[str, Any]


class BagDownloadProbe:
    """维测脚本：探索 getObsId 与 download 的兼容组合，并给出建议方案。"""

    def __init__(self, config_path: str | Path, timeout_sec: int = 30):
        cfg_path = Path(config_path)
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        self.base_url = str(cfg["base_url"]).rstrip("/")
        self.verify_ssl = bool(cfg.get("verify_ssl", False))
        self.timeout_sec = int(timeout_sec or cfg.get("timeout_sec", 30))
        self.browser_headers = dict(cfg.get("browser_headers", {}))

        self.session = requests.Session()
        self.common_headers = {
            "Accept-Language": "zh-CN",
            **self.browser_headers,
        }

    def _headers_for(
        self,
        platform: str,
        json_body: bool = True,
        override_headers: dict[str, Any] | None = None,
        exact_headers: bool = False,
    ) -> dict[str, str]:
        if exact_headers and override_headers:
            headers = {str(k): str(v) for k, v in override_headers.items() if v is not None}
        else:
            headers = dict(self.common_headers)
            if override_headers:
                headers.update({str(k): str(v) for k, v in override_headers.items() if v is not None})

        if "Deepdata-platform" not in headers:
            headers["Deepdata-platform"] = platform
        if json_body and "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        return headers

    def _post(
        self,
        url: str,
        payload: dict[str, Any],
        platform: str,
        override_headers: dict[str, Any] | None = None,
        exact_headers: bool = False,
    ) -> dict[str, Any]:
        t0 = time.time()
        resp = self.session.post(
            url,
            json=payload,
            headers=self._headers_for(
                platform,
                json_body=True,
                override_headers=override_headers,
                exact_headers=exact_headers,
            ),
            verify=self.verify_ssl,
            timeout=self.timeout_sec,
        )
        elapsed = round((time.time() - t0) * 1000, 1)
        return {
            "status_code": resp.status_code,
            "elapsed_ms": elapsed,
            "json": self._safe_json(resp),
            "text_head": resp.text[:500],
        }

    @staticmethod
    def _safe_json(resp: requests.Response) -> Any:
        try:
            return resp.json()
        except Exception:
            return None

    @staticmethod
    def _inspect_download_content(content: bytes) -> dict[str, Any]:
        info: dict[str, Any] = {
            "size": len(content),
            "prefix_hex": content[:32].hex(),
            "prefix_text": content[:64].decode("utf-8", errors="replace").replace("\n", "\\n"),
            "looks_like_rosbag": content.startswith(b"#ROSBAG"),
            "looks_like_zip_lfh": content.startswith(b"PK\x03\x04"),
            "looks_like_zip_eocd": content.startswith(b"PK\x05\x06"),
        }
        if info["looks_like_zip_lfh"] or info["looks_like_zip_eocd"]:
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as zf:
                    names = zf.namelist()
                    info["zip_members"] = names
                    bag_members = [x for x in names if x.endswith(".bag")]
                    info["zip_bag_members"] = bag_members
                    if bag_members:
                        bag_data = zf.read(bag_members[0])
                        info["zip_first_bag_size"] = len(bag_data)
                        info["zip_first_bag_magic_ok"] = bag_data.startswith(b"#ROSBAG")
            except Exception as e:
                info["zip_error"] = str(e)

        info["is_empty_zip_22_bytes"] = bool(info["looks_like_zip_eocd"] and len(content) == 22)
        return info

    @staticmethod
    def _to_path_variants(bucket: str, remote_path: str) -> dict[str, str]:
        remote_path = remote_path.strip()
        if remote_path.startswith("obs://"):
            core = remote_path[6:]
            if "/" in core:
                _, rest = core.split("/", 1)
            else:
                rest = ""
            return {
                "obs_uri": remote_path,
                "slash_bucket": f"/{core}",
                "slash_no_bucket": f"/{rest}",
                "no_prefix": core,
            }

        rp = remote_path if remote_path.startswith("/") else f"/{remote_path}"
        compact = rp.lstrip("/")
        parts = compact.split("/", 1)
        has_bucket = parts[0] == bucket
        rest = parts[1] if has_bucket and len(parts) > 1 else compact

        return {
            "obs_uri": f"obs://{bucket}/{rest}",
            "slash_bucket": rp if has_bucket else f"/{bucket}/{compact}",
            "slash_no_bucket": f"/{rest}",
            "no_prefix": f"{bucket}/{rest}",
        }

    @staticmethod
    def _extract_result_field(detail_json: Any, key: str, default: Any = None) -> Any:
        if isinstance(detail_json, dict):
            return detail_json.get(key, default)
        return default

    def resolve_download_menu(
        self,
        bucket: str,
        request_headers: dict[str, Any] | None = None,
        exact_headers: bool = False,
    ) -> ProbeResult:
        url = f"{self.base_url}/rivulet/v1/dataDownload/downloadMenu"
        payload = {"bucketName": bucket}
        detail = self._post(
            url,
            payload,
            platform="rivulet",
            override_headers=request_headers,
            exact_headers=exact_headers,
        )
        data = detail.get("json") or {}
        obs_download_url = ((data.get("data") or {}).get("obs_download_url")) if isinstance(data, dict) else None
        detail["obs_download_url"] = obs_download_url
        return ProbeResult(name="downloadMenu", ok=bool(obs_download_url), detail=detail)

    def probe_get_obs_id(
        self,
        bucket: str,
        remote_path: str,
        user_name: str,
        data_type: list[str] | None = None,
        dataset_name: str | None = None,
        file_size: int | None = None,
        request_headers: dict[str, Any] | None = None,
        exact_headers: bool = False,
    ) -> list[ProbeResult]:
        url = f"{self.base_url}/rivulet/v1/dataDownload/getObsId"
        data_type = data_type if data_type is not None else []

        path_variants = self._to_path_variants(bucket=bucket, remote_path=remote_path)
        file_name = Path(remote_path).name

        def _mk_payload(path_value: str, include_name: bool, include_size: bool, include_user: bool, include_dataset: bool) -> dict[str, Any]:
            f_item: dict[str, Any] = {"path": path_value}
            if include_name:
                f_item["name"] = file_name
                f_item["type"] = "file"
            if include_size and file_size is not None:
                f_item["size"] = int(file_size)

            payload: dict[str, Any] = {
                "files": [f_item],
                "bucket": bucket,
            }
            if include_user:
                payload["userName"] = user_name
                payload["dataType"] = data_type
            if include_dataset and dataset_name:
                payload["datasetName"] = dataset_name
            return payload

        candidates: list[tuple[str, dict[str, Any]]] = []
        for path_name, path_value in path_variants.items():
            candidates.append((f"minimal::{path_name}", _mk_payload(path_value, False, False, False, False)))
            candidates.append((f"browser_like::{path_name}", _mk_payload(path_value, True, False, True, False)))
            if file_size is not None:
                candidates.append((f"browser_like_size::{path_name}", _mk_payload(path_value, True, True, True, False)))
            if dataset_name:
                candidates.append((f"minimal_dataset::{path_name}", _mk_payload(path_value, False, False, False, True)))

        results: list[ProbeResult] = []
        for name, payload in candidates:
            detail = self._post(
                url,
                payload,
                platform="rivulet",
                override_headers=request_headers,
                exact_headers=exact_headers,
            )
            detail["request_payload"] = payload
            detail["path_style"] = name.split("::")[-1]
            obs_id = self._extract_result_field(detail.get("json"), "result")
            detail["obs_id"] = obs_id
            ok = bool(obs_id)
            results.append(ProbeResult(name=f"getObsId::{name}", ok=ok, detail=detail))
        return results

    def probe_download(
        self,
        obs_download_url: str,
        obs_id: str,
        request_name: str,
        request_headers: dict[str, Any] | None = None,
        exact_headers: bool = False,
    ) -> ProbeResult:
        url = obs_download_url.rstrip("/") + "/obs/v1/files/download?opid=" + obs_id
        headers = self._headers_for(
            "rivulet",
            json_body=False,
            override_headers=request_headers,
            exact_headers=exact_headers,
        )
        t0 = time.time()
        resp = self.session.get(url, headers=headers, verify=self.verify_ssl, timeout=self.timeout_sec)
        elapsed = round((time.time() - t0) * 1000, 1)
        inspect = self._inspect_download_content(resp.content)

        quality = "invalid"
        if resp.status_code == 200:
            if inspect.get("looks_like_rosbag"):
                quality = "rosbag"
            elif inspect.get("zip_bag_members"):
                quality = "zip_with_bag"
            elif inspect.get("is_empty_zip_22_bytes"):
                quality = "empty_zip_22"
            elif inspect.get("size") == 0:
                quality = "empty_body"
            else:
                quality = "unknown_body"

        detail = {
            "request_name": request_name,
            "url": url,
            "status_code": resp.status_code,
            "elapsed_ms": elapsed,
            "headers": {
                "content_type": resp.headers.get("Content-Type"),
                "content_length": resp.headers.get("Content-Length"),
                "content_disposition": resp.headers.get("Content-Disposition"),
            },
            "quality": quality,
            "inspect": inspect,
        }

        ok = quality in {"rosbag", "zip_with_bag"}
        return ProbeResult(name=f"download::{request_name}", ok=ok, detail=detail)

    @staticmethod
    def _summarize_strategy(results: list[dict[str, Any]]) -> dict[str, Any]:
        dl_items = [x for x in results if str(x.get("name", "")).startswith("download::")]
        success = [x for x in dl_items if bool(x.get("ok"))]
        if success:
            quality_rank = {"rosbag": 3, "zip_with_bag": 2}
            success_sorted = sorted(
                success,
                key=lambda x: quality_rank.get(str((x.get("detail") or {}).get("quality")), 0),
                reverse=True,
            )
            best = success_sorted[0]
            best_name = str(best.get("name") or "")
            source_case = best_name.split("download::", 1)[-1] if "download::" in best_name else best_name
            return {
                "status": "has_compatible_scheme",
                "recommended_download_case": best_name,
                "recommended_get_obs_id_case": f"getObsId::{source_case}" if source_case != "manual_obs_id" else None,
                "recommended_quality": ((best.get("detail") or {}).get("quality")),
                "all_success_cases": [
                    {
                        "download_case": str(x.get("name") or ""),
                        "quality": (x.get("detail") or {}).get("quality"),
                    }
                    for x in success_sorted
                ],
                "notes": "优先选择 rosbag 直出；若仅有 zip_with_bag，可在主流程解压后继续校验 #ROSBAG。",
            }

        qualities: dict[str, int] = {}
        for item in dl_items:
            q = str(((item.get("detail") or {}).get("quality") or "unknown"))
            qualities[q] = qualities.get(q, 0) + 1

        return {
            "status": "no_compatible_scheme",
            "quality_counter": qualities,
            "notes": "未探测到可用下载方案；请核查 token、路径形态、opid 时效。",
        }

    def run(
        self,
        bucket: str,
        remote_path: str,
        user_name: str,
        out_json: str | Path,
        obs_download_url: str | None = None,
        obs_id: str | None = None,
        data_type: list[str] | None = None,
        dataset_name: str | None = None,
        file_size: int | None = None,
        getobsid_headers: dict[str, Any] | None = None,
        download_headers: dict[str, Any] | None = None,
        menu_headers: dict[str, Any] | None = None,
        exact_headers: bool = False,
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            "input": {
                "bucket": bucket,
                "remote_path": remote_path,
                "user_name": user_name,
                "dataset_name": dataset_name,
                "obs_download_url": obs_download_url,
                "obs_id": obs_id,
                "data_type": data_type or [],
                "file_size": file_size,
                "exact_headers": exact_headers,
                "menu_header_keys": sorted(list((menu_headers or {}).keys())),
                "getobsid_header_keys": sorted(list((getobsid_headers or {}).keys())),
                "download_header_keys": sorted(list((download_headers or {}).keys())),
            },
            "results": [],
        }

        menu_result = self.resolve_download_menu(
            bucket=bucket,
            request_headers=menu_headers,
            exact_headers=exact_headers,
        )
        report["results"].append(asdict(menu_result))
        if not obs_download_url:
            obs_download_url = menu_result.detail.get("obs_download_url")

        obs_results = self.probe_get_obs_id(
            bucket=bucket,
            remote_path=remote_path,
            user_name=user_name,
            data_type=data_type,
            dataset_name=dataset_name,
            file_size=file_size,
            request_headers=getobsid_headers,
            exact_headers=exact_headers,
        )
        report["results"].extend(asdict(x) for x in obs_results)

        download_candidates: list[tuple[str, str]] = []
        if obs_id:
            download_candidates.append(("manual_obs_id", obs_id))

        for r in obs_results:
            found_obs_id = r.detail.get("obs_id")
            if found_obs_id:
                alias = r.name.split("getObsId::", 1)[-1]
                download_candidates.append((alias, str(found_obs_id)))

        dedup: dict[str, str] = {}
        for alias, oid in download_candidates:
            dedup.setdefault(oid, alias)

        if obs_download_url:
            for oid, alias in dedup.items():
                dl = self.probe_download(
                    obs_download_url=obs_download_url,
                    obs_id=oid,
                    request_name=alias,
                    request_headers=download_headers,
                    exact_headers=exact_headers,
                )
                report["results"].append(asdict(dl))
        else:
            report["results"].append(
                asdict(
                    ProbeResult(
                        name="download::skipped",
                        ok=False,
                        detail={"reason": "downloadMenu 未解析到 obs_download_url，跳过下载探测"},
                    )
                )
            )

        report["analysis"] = self._summarize_strategy(report["results"])

        out_path = Path(out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="bag 兼容下载维测脚本")
    p.add_argument("--config", default="config.json", help="配置文件路径")
    p.add_argument("--bucket", required=True, help="OBS bucket")
    p.add_argument("--remote-path", required=True, help="OBS 文件路径，支持 obs:// 或 /bucket/... 形式")
    p.add_argument("--user-name", required=True, help="请求参数 userName")
    p.add_argument("--dataset-name", default=None, help="可选 datasetName")
    p.add_argument("--file-size", type=int, default=None, help="可选：文件大小，传入后会纳入 browser_like_size 负载")
    p.add_argument("--obs-id", default=None, help="可选：手动指定 obs_id 直接验证下载")
    p.add_argument("--obs-download-url", default=None, help="可选：手动指定 downloadMenu 返回的 obs_download_url")
    p.add_argument("--data-type", default="", help="逗号分隔的 dataType 列表，如 raw,bag")
    p.add_argument("--headers-file", default=None, help="可选：通用请求头 JSON 文件（同时用于 menu/getObsId/download）")
    p.add_argument("--menu-headers-file", default=None, help="可选：downloadMenu 请求头 JSON 文件")
    p.add_argument("--getobsid-headers-file", default=None, help="可选：getObsId 请求头 JSON 文件")
    p.add_argument("--download-headers-file", default=None, help="可选：download 请求头 JSON 文件")
    p.add_argument("--exact-headers", action="store_true", help="若开启，仅使用提供的 headers（不自动合并 config.browser_headers）")
    p.add_argument("--timeout-sec", type=int, default=30, help="请求超时秒数")
    p.add_argument("--out", default="outputs/bag_probe_report.json", help="维测报告输出路径")
    return p


def _load_headers_json(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    p = Path(path)
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"headers 文件必须是 JSON object: {path}")
    return data


def main() -> None:
    args = build_parser().parse_args()
    data_type = [x.strip() for x in args.data_type.split(",") if x.strip()]
    common_headers = _load_headers_json(args.headers_file)
    menu_headers = _load_headers_json(args.menu_headers_file) or common_headers
    getobsid_headers = _load_headers_json(args.getobsid_headers_file) or common_headers
    download_headers = _load_headers_json(args.download_headers_file) or common_headers

    probe = BagDownloadProbe(config_path=args.config, timeout_sec=args.timeout_sec)
    report = probe.run(
        bucket=args.bucket,
        remote_path=args.remote_path,
        user_name=args.user_name,
        out_json=args.out,
        obs_download_url=args.obs_download_url,
        obs_id=args.obs_id,
        data_type=data_type,
        dataset_name=args.dataset_name,
        file_size=args.file_size,
        menu_headers=menu_headers,
        getobsid_headers=getobsid_headers,
        download_headers=download_headers,
        exact_headers=args.exact_headers,
    )

    print(f"[INFO] 维测完成，报告: {args.out}")
    for item in report.get("results", []):
        state = "OK" if item.get("ok") else "FAIL"
        print(f"[{state}] {item.get('name')}")
    print(f"[INFO] 结论: {json.dumps(report.get('analysis', {}), ensure_ascii=False)}")


if __name__ == "__main__":
    main()
