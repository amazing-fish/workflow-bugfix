# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import mimetypes
import re
import ssl
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import truststore


EXT_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


def log_info(msg: str) -> None:
    print(f"[INFO] {msg}")


def log_warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def log_error(msg: str) -> None:
    print(f"[ERROR] {msg}")


def pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def save_json(obj: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pretty(obj), encoding="utf-8")


def is_none_like_output(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() == "none"
    return False


def guess_mime_type(path: Path) -> str:
    return (
        mimetypes.guess_type(path.name)[0]
        or EXT_TO_MIME.get(path.suffix.lower())
        or "application/octet-stream"
    )


def build_transport() -> httpx.HTTPTransport:
    ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return httpx.HTTPTransport(verify=ssl_context)


def build_timeout(cfg: dict[str, Any]) -> httpx.Timeout:
    runtime = cfg.get("runtime", {})
    return httpx.Timeout(
        connect=runtime.get("connect_timeout_sec", 30),
        read=runtime.get("read_timeout_sec", 600),
        write=runtime.get("write_timeout_sec", 120),
        pool=runtime.get("pool_timeout_sec", 60),
    )


def get_by_selector(obj: Any, selector: list[str]) -> Any:
    cur = obj
    for key in selector:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def parse_sse_line(line: str) -> tuple[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    if line.startswith("event:"):
        return ("event_name", line[len("event:"):].strip())
    if line.startswith("data:"):
        raw = line[len("data:"):].strip()
        try:
            return ("data", json.loads(raw))
        except Exception:
            return ("data_raw", raw)
    return ("other", line)


def extract_input_contract(parameters: dict) -> dict[str, Any]:
    result: dict[str, Any] = {
        "query": {"exists": False, "required": False, "max_length": None, "default": None},
        "images": {
            "exists": False,
            "required": False,
            "max_length": None,
            "allowed_file_types": [],
            "allowed_upload_methods": [],
        },
        "system_parameters": parameters.get("system_parameters", {}),
    }

    for item in parameters.get("user_input_form", []):
        if "paragraph" in item:
            p = item["paragraph"]
            if p.get("variable") == "query":
                result["query"] = {
                    "exists": True,
                    "required": p.get("required", False),
                    "max_length": p.get("max_length"),
                    "default": p.get("default"),
                }
        if "file-list" in item:
            f = item["file-list"]
            if f.get("variable") == "images":
                result["images"] = {
                    "exists": True,
                    "required": f.get("required", False),
                    "max_length": f.get("max_length"),
                    "allowed_file_types": f.get("allowed_file_types", []),
                    "allowed_upload_methods": f.get("allowed_file_upload_methods", []),
                }
    return result


def validate_final_schema(cfg: dict[str, Any], obj: dict[str, Any] | None) -> dict[str, Any]:
    fields = cfg.get("workflow_contract", {}).get("structured_fields", [])
    report = {
        "ok": True,
        "missing_fields": [],
        "type_errors": {},
        "enum_errors": {},
    }
    if not isinstance(obj, dict):
        report["ok"] = False
        report["missing_fields"] = [f["name"] for f in fields if f.get("required", False)]
        return report

    type_map = {
        "string": str,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for field in fields:
        name = field["name"]
        required = field.get("required", False)
        declared_type = field.get("type")
        allowed_enum = field.get("enum")
        if name not in obj:
            if required:
                report["ok"] = False
                report["missing_fields"].append(name)
            continue
        value = obj[name]
        expected_type = type_map.get(declared_type)
        if expected_type is not None and not isinstance(value, expected_type):
            report["ok"] = False
            report["type_errors"][name] = {"expected": declared_type, "actual": type(value).__name__}
        if allowed_enum and value not in allowed_enum:
            report["ok"] = False
            report["enum_errors"][name] = {"allowed": allowed_enum, "actual": value}
    return report


def normalize_distance_range(value: Any, distance_unit: str = "cm") -> dict[str, Any]:
    result = {"raw": value, "min_m": None, "max_m": None, "mid_m": None}
    if value is None:
        return result
    if isinstance(value, (int, float)):
        v = float(value)
        if distance_unit == "cm":
            v /= 100.0
        result["min_m"] = v
        result["max_m"] = v
        result["mid_m"] = v
        return result
    if isinstance(value, str):
        s = value.strip().lower().replace(" ", "")
        try:
            if "-" in s:
                left, right = s.split("-", 1)
                min_v = float(left)
                max_v = float(right)
                if distance_unit == "cm":
                    min_v /= 100.0
                    max_v /= 100.0
                result["min_m"] = min_v
                result["max_m"] = max_v
                result["mid_m"] = (min_v + max_v) / 2.0
                return result
            v = float(s)
            if distance_unit == "cm":
                v /= 100.0
            result["min_m"] = v
            result["max_m"] = v
            result["mid_m"] = v
            return result
        except Exception:
            return result
    return result


def normalize_final_output(cfg: dict[str, Any], obj: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    norm_cfg = cfg.get("normalization", {})
    collision_field = norm_cfg.get("collision_field", "collision_pred")
    distance_field = norm_cfg.get("distance_field", "nearest_obstacle_distance_cm")
    summary_field = norm_cfg.get("summary_field", "summary")
    key_image_field = norm_cfg.get("key_image_field", "key_image")
    distance_unit = norm_cfg.get("distance_unit", "cm")
    empty_key_values = {str(x).lower() for x in norm_cfg.get("key_image_empty_values", [""])}

    distance_info = normalize_distance_range(obj.get(distance_field), distance_unit=distance_unit)
    key_image = obj.get(key_image_field)
    if isinstance(key_image, str) and key_image.strip().lower() in empty_key_values:
        key_image = ""

    return {
        "collision_pred": obj.get(collision_field),
        "nearest_obstacle_distance_range_raw": distance_info["raw"],
        "nearest_obstacle_distance_min_m": distance_info["min_m"],
        "nearest_obstacle_distance_max_m": distance_info["max_m"],
        "nearest_obstacle_distance_m": distance_info["mid_m"],
        "summary": obj.get(summary_field),
        "key_image": key_image,
        "raw": obj,
    }


class SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class WorkflowAIProcessor:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.ai_cfg = cfg.get("ai", {})
        self.api_cfg = self.ai_cfg.get("api", {})
        self.runtime_cfg = self.ai_cfg.get("runtime", {})
        self.selection_cfg = self.ai_cfg.get("image_selection", {})
        self.input_cfg = self.ai_cfg.get("input", {})

        self._validate_api_cfg()
        self._cache_lock = threading.Lock()
        self.parameters: dict[str, Any] | None = None
        self.contract: dict[str, Any] | None = None

    def close(self) -> None:
        return None

    def enabled(self) -> bool:
        return bool(self.ai_cfg.get("enabled", False))

    def prepare(self) -> None:
        if self.parameters is not None and self.contract is not None:
            return
        with self._cache_lock:
            if self.parameters is not None and self.contract is not None:
                return
            log_info("读取 AI workflow parameters")
            with self._build_client() as client:
                resp = client.get(
                    f"{self.api_cfg['base_url'].rstrip('/')}/parameters",
                    headers=self._auth_headers(json_mode=False),
                )
                resp.raise_for_status()
                self.parameters = resp.json()
                self.contract = extract_input_contract(self.parameters)

    def run_for_task_sequence(
        self,
        row_meta: dict[str, Any],
        task_meta: dict[str, Any],
        task_dir: str | Path,
    ) -> dict[str, Any]:
        self.prepare()
        task_dir = Path(task_dir)

        manifest = self._load_manifest(task_dir)
        sample_names = self._collect_sample_sequence(manifest)
        if not sample_names:
            raise RuntimeError(f"task={task_dir.name} 未发现可处理 sample")

        sequence_results: list[dict[str, Any]] = []
        started = time.time()

        if self.runtime_cfg.get("save_parameters_once", False):
            save_json(self.parameters, task_dir / "parameters.json")
            save_json(self.contract, task_dir / "input_contract.json")

        with self._build_client() as client:
            for sample_index, sample_name in enumerate(sample_names, start=1):
                sample_dir = task_dir / sample_name
                ai_dir = sample_dir / "ai"
                ai_dir.mkdir(parents=True, exist_ok=True)
                try:
                    images = self._collect_images_for_sample(manifest, sample_name)
                    self._validate_selected_images(images)
                    query = self._build_query(
                        row_meta=row_meta,
                        task_meta=task_meta,
                        images=images,
                        sample_index=sample_index,
                        sample_name=sample_name,
                    )
                    uploaded_files = self._upload_files(client, images)
                    payload = self._build_workflow_payload(query, uploaded_files)

                    save_json([
                        {"path": str(p), "name": p.name, "size": p.stat().st_size if p.exists() else None}
                        for p in images
                    ], ai_dir / "selected_images.json")
                    save_json(uploaded_files, ai_dir / "uploaded_files.json")
                    save_json(payload, ai_dir / "workflow_payload.json")

                    max_retry = 3
                    stream_result: dict[str, Any] | None = None
                    schema_report: dict[str, Any] | None = None
                    normalized: dict[str, Any] | None = None
                    collision_pred = None
                    retry_count = 0

                    for attempt in range(max_retry + 1):
                        stream_result = self._run_workflow_streaming(client, payload, ai_dir)
                        schema_report = validate_final_schema(self.ai_cfg, stream_result.get("final_structured_output"))
                        normalized = normalize_final_output(self.ai_cfg, stream_result.get("final_structured_output"))
                        collision_pred = normalized.get("collision_pred") if isinstance(normalized, dict) else None

                        if not is_none_like_output(collision_pred):
                            break
                        if attempt < max_retry:
                            retry_count += 1
                            log_warn(
                                f"{row_meta.get('row_id')}/{task_meta.get('task_id')}/{sample_name} "
                                f"输出为 None，触发重试 {retry_count}/{max_retry}"
                            )

                    save_json(stream_result, ai_dir / "workflow_stream_result.json")
                    save_json(schema_report, ai_dir / "workflow_final_schema_report.json")
                    save_json(normalized, ai_dir / "workflow_final_normalized.json")

                    sample_result = {
                        "sample_index": sample_index,
                        "sample_name": sample_name,
                        "status": "ok" if schema_report.get("ok") else "schema_invalid",
                        "collision_pred": collision_pred,
                        "none_retry_count": retry_count,
                        "selected_count": len(images),
                        "schema_ok": schema_report.get("ok"),
                        "nearest_obstacle_distance_m": (normalized or {}).get("nearest_obstacle_distance_m"),
                        "summary": (normalized or {}).get("summary"),
                        "detail_paths": {
                            "selected_images": str(ai_dir / "selected_images.json"),
                            "uploaded_files": str(ai_dir / "uploaded_files.json"),
                            "workflow_payload": str(ai_dir / "workflow_payload.json"),
                            "workflow_stream_result": str(ai_dir / "workflow_stream_result.json"),
                            "schema_report": str(ai_dir / "workflow_final_schema_report.json"),
                            "normalized": str(ai_dir / "workflow_final_normalized.json"),
                        },
                    }
                    save_json(sample_result, ai_dir / "sample_result_summary.json")
                    sequence_results.append(sample_result)
                    log_info(f"{row_meta.get('row_id')}/{task_meta.get('task_id')}/{sample_name} AI 完成: {collision_pred}")
                except Exception as e:
                    sample_result = {
                        "sample_index": sample_index,
                        "sample_name": sample_name,
                        "status": "failed",
                        "error": str(e),
                        "collision_pred": None,
                    }
                    save_json(sample_result, ai_dir / "sample_result_summary.json")
                    sequence_results.append(sample_result)
                    log_error(f"{row_meta.get('row_id')}/{task_meta.get('task_id')}/{sample_name} AI 失败: {e}")

        aggregate = self._aggregate_sequence_results(sequence_results)
        result = {
            "status": "ok" if aggregate["failed_samples"] == 0 else "partial_failed",
            "elapsed_sec": round(time.time() - started, 3),
            "sample_count": len(sample_names),
            "sequence_results": sequence_results,
            "aggregate": aggregate,
            "result_dir": str(task_dir),
            "detail_note": "sample级详情请查看每个 sample*/ai 目录下的明细文件",
        }
        cleanup_report = self._cleanup_no_samples(task_dir, sequence_results)
        if cleanup_report is not None:
            result["cleanup"] = cleanup_report
        save_json(result, task_dir / "ai_result_summary.json")
        return result

    # ---------------- internal ----------------

    def _validate_api_cfg(self) -> None:
        required = ["base_url", "api_key", "user_id", "response_mode"]
        for key in required:
            if not self.api_cfg.get(key):
                raise ValueError(f"config.ai.api 缺少必要字段: {key}")
        if self.api_cfg.get("response_mode") != "streaming":
            raise ValueError("当前集成版本仅实现 streaming")

    def _build_client(self) -> httpx.Client:
        return httpx.Client(
            transport=build_transport(),
            follow_redirects=True,
            timeout=build_timeout(self.ai_cfg),
        )

    def _auth_headers(self, json_mode: bool = True) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_cfg['api_key']}"}
        if json_mode:
            headers["Content-Type"] = "application/json"
        return headers

    def _load_manifest(self, task_dir: Path) -> dict[str, Any]:
        manifest_path = task_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"找不到 frames manifest: {manifest_path}")
        with open(manifest_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _collect_sample_sequence(self, manifest: dict[str, Any]) -> list[str]:
        bags = manifest.get("bags", {})
        all_samples = set()
        for _, info in bags.items():
            for frame in info.get("frames", []):
                sample_name = frame.get("sample")
                if frame.get("status") == "ok" and sample_name:
                    all_samples.add(sample_name)
        def sort_key(name: str) -> tuple[int, str]:
            m = re.search(r"(\d+)", name)
            return (int(m.group(1)) if m else 10**9, name)
        samples = sorted(all_samples, key=sort_key)

        mode = self.selection_cfg.get("mode", "sample_sequence_per_camera")
        if mode == "sample_sequence_per_camera":
            return samples
        if mode == "center_frame_per_camera":
            before_frames = int(manifest.get("before_frames", 4))
            center_no = before_frames + 1
            ext = Path(samples[0]).suffix if samples else ".png"
            return [f"sample{center_no:02d}{ext}"]
        if mode == "sample_name_per_camera":
            sample_names = list(self.selection_cfg.get("sample_names", []))
            return sample_names
        raise ValueError(f"不支持的 ai.image_selection.mode: {mode}")

    def _collect_images_for_sample(self, manifest: dict[str, Any], sample_name: str) -> list[Path]:
        bags = manifest.get("bags", {})
        selected: list[Path] = []
        for camera_name in sorted(bags.keys()):
            info = bags[camera_name]
            for frame in info.get("frames", []):
                if frame.get("status") == "ok" and frame.get("sample") == sample_name:
                    image_path = frame.get("image_path")
                    if image_path:
                        selected.append(Path(image_path))
                    break
        selected = [p for p in selected if p.exists()]
        selected.sort(key=lambda p: str(p))
        if not selected:
            raise RuntimeError(f"sample={sample_name} 未选出可上传图片")
        return selected

    def _validate_selected_images(self, images: list[Path]) -> None:
        contract = self.contract or {}
        if not contract.get("images", {}).get("exists", False):
            raise ValueError("workflow 未声明 images 输入字段")
        imax = contract.get("images", {}).get("max_length")
        if imax is not None and len(images) > imax:
            if self.selection_cfg.get("trim_to_max_length", False):
                del images[imax:]
            else:
                raise ValueError(f"图片数量超限: {len(images)} > {imax}")
        image_limit_mb = contract.get("system_parameters", {}).get("image_file_size_limit")
        if image_limit_mb is not None:
            for p in images:
                size_mb = p.stat().st_size / 1024 / 1024
                if size_mb > image_limit_mb:
                    raise ValueError(f"图片超过大小限制: {p}, {size_mb:.2f}MB > {image_limit_mb}MB")

    def _build_query(
        self,
        row_meta: dict[str, Any],
        task_meta: dict[str, Any],
        images: list[Path],
        sample_index: int,
        sample_name: str,
    ) -> str:
        template = self.input_cfg.get("query_template") or self.input_cfg.get("query") or ""
        if not template:
            default = (self.contract or {}).get("query", {}).get("default")
            template = default or "根据输入图片分析当前 case"
        data = SafeDict(
            row_id=row_meta.get("row_id", ""),
            excel_row=row_meta.get("excel_row", ""),
            task_id=task_meta.get("task_id", ""),
            target_ts=task_meta.get("target_ts", ""),
            sample_index=sample_index,
            sample_name=sample_name,
            image_count=len(images),
            image_names=", ".join(p.name for p in images),
        )
        query = template.format_map(data)
        qmax = (self.contract or {}).get("query", {}).get("max_length")
        if qmax is not None and len(query) > qmax:
            raise ValueError(f"query 超长: {len(query)} > {qmax}")
        return query

    def _upload_files(self, client: httpx.Client, images: list[Path]) -> list[dict[str, Any]]:
        results = []
        upload_delay_sec = float(self.runtime_cfg.get("upload_delay_sec", 0.0))
        for image_path in images:
            mime_type = guess_mime_type(image_path)
            with image_path.open("rb") as f:
                resp = client.post(
                    f"{self.api_cfg['base_url'].rstrip('/')}/files/upload",
                    headers={"Authorization": f"Bearer {self.api_cfg['api_key']}"},
                    files={"file": (image_path.name, f, mime_type)},
                    data={"user": self.api_cfg["user_id"]},
                )
            resp.raise_for_status()
            data = resp.json()
            results.append({
                "local_path": str(image_path),
                "file_id": data["id"],
                "name": data.get("name", image_path.name),
                "size": data.get("size"),
                "extension": data.get("extension"),
                "mime_type": data.get("mime_type"),
                "source_url": data.get("source_url"),
                "created_at": data.get("created_at"),
            })
            if upload_delay_sec > 0:
                time.sleep(upload_delay_sec)
        return results

    def _build_workflow_payload(self, query: str, uploaded_files: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "inputs": {
                "query": query,
                "images": [
                    {"type": "image", "transfer_method": "local_file", "upload_file_id": f["file_id"]}
                    for f in uploaded_files
                ],
            },
            "response_mode": self.api_cfg["response_mode"],
            "user": self.api_cfg["user_id"],
        }

    def _run_workflow_streaming(self, client: httpx.Client, payload: dict[str, Any], ai_dir: Path) -> dict[str, Any]:
        current_event_name: str | None = None
        keep_text_chunks = bool(self.runtime_cfg.get("keep_text_chunks_in_memory", False))
        keep_raw_events = bool(self.runtime_cfg.get("keep_raw_events_in_memory", False))
        save_raw_events_ndjson = bool(self.runtime_cfg.get("save_raw_events_ndjson", True))

        raw_events_path = ai_dir / "workflow_raw_events.ndjson"
        raw_fp = raw_events_path.open("w", encoding="utf-8") if save_raw_events_ndjson else None

        result: dict[str, Any] = {
            "workflow_run_id": None,
            "task_id": None,
            "workflow_id": None,
            "status": None,
            "events_count": 0,
            "ping_count": 0,
            "nodes": [],
            "text_chunks": [] if keep_text_chunks else None,
            "raw_events": [] if keep_raw_events else None,
            "final_outputs": None,
            "final_structured_output": None,
            "total_tokens": None,
            "total_steps": None,
            "elapsed_time": None,
            "created_at": None,
            "finished_at": None,
        }

        try:
            with client.stream(
                "POST",
                f"{self.api_cfg['base_url'].rstrip('/')}/workflows/run",
                headers=self._auth_headers(json_mode=True),
                json=payload,
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    parsed = parse_sse_line(line)
                    if parsed is None:
                        continue
                    kind, value = parsed
                    if kind == "event_name":
                        current_event_name = value
                        if value == "ping":
                            result["ping_count"] += 1
                        continue
                    if kind != "data":
                        continue

                    evt = value
                    result["events_count"] += 1
                    if raw_fp is not None:
                        raw_fp.write(json.dumps(evt, ensure_ascii=False) + "\n")
                    if keep_raw_events:
                        result["raw_events"].append(evt)

                    event_type = evt.get("event") or current_event_name
                    workflow_run_id = evt.get("workflow_run_id")
                    task_id = evt.get("task_id")
                    data = evt.get("data") or {}
                    if workflow_run_id:
                        result["workflow_run_id"] = workflow_run_id
                    if task_id:
                        result["task_id"] = task_id

                    if event_type == "workflow_started":
                        result["workflow_id"] = data.get("workflow_id")
                    elif event_type == "node_finished":
                        outputs = data.get("outputs") or {}
                        process_data = data.get("process_data") or {}
                        usage = outputs.get("usage") if isinstance(outputs, dict) else None
                        if not isinstance(usage, dict):
                            usage = process_data.get("usage") if isinstance(process_data, dict) else None
                        node_summary = {
                            "node_id": data.get("node_id"),
                            "title": data.get("title"),
                            "node_type": data.get("node_type"),
                            "status": data.get("status"),
                            "started_at": data.get("created_at"),
                            "finished_at": data.get("finished_at"),
                            "elapsed_time": data.get("elapsed_time"),
                            "outputs_keys": list(outputs.keys()) if isinstance(outputs, dict) else None,
                            "usage": usage,
                            "model_name": outputs.get("model_name") if isinstance(outputs, dict) else None,
                            "model_provider": outputs.get("model_provider") if isinstance(outputs, dict) else None,
                        }
                        result["nodes"].append(node_summary)
                        if node_summary.get("node_type") == "end":
                            selector = self.ai_cfg.get("output", {}).get("final_output_selector", ["output"])
                            selected = get_by_selector(outputs, selector)
                            if isinstance(selected, dict):
                                result["final_outputs"] = outputs
                                result["final_structured_output"] = selected
                        if result["final_structured_output"] is None and isinstance(outputs, dict):
                            structured_output = outputs.get("structured_output")
                            if isinstance(structured_output, dict):
                                result["final_structured_output"] = structured_output
                    elif event_type == "text_chunk":
                        text = (data or {}).get("text", "")
                        if text and keep_text_chunks:
                            result["text_chunks"].append(text)
                    elif event_type == "workflow_finished":
                        result["status"] = data.get("status")
                        result["elapsed_time"] = data.get("elapsed_time")
                        result["total_tokens"] = data.get("total_tokens")
                        result["total_steps"] = data.get("total_steps")
                        result["created_at"] = data.get("created_at")
                        result["finished_at"] = data.get("finished_at")
                        result["final_outputs"] = data.get("outputs")
                        if result["final_structured_output"] is None:
                            selector = self.ai_cfg.get("output", {}).get("final_output_selector", ["output"])
                            selected = get_by_selector(result["final_outputs"], selector)
                            if isinstance(selected, dict):
                                result["final_structured_output"] = selected
        finally:
            if raw_fp is not None:
                raw_fp.close()
        return result

    def _aggregate_sequence_results(self, sequence_results: list[dict[str, Any]]) -> dict[str, Any]:
        yes_count = 0
        no_count = 0
        suspected_count = 0
        failed_samples = 0
        valid_samples = 0
        result_sequence: list[str | None] = []
        detailed_sequence: list[dict[str, Any]] = []

        for item in sequence_results:
            pred = item.get("collision_pred")
            status = item.get("status")
            if pred == "是":
                yes_count += 1
                valid_samples += 1
            elif pred == "否":
                no_count += 1
                valid_samples += 1
            elif pred == "疑似":
                suspected_count += 1
                valid_samples += 1
            else:
                if status == "failed":
                    failed_samples += 1
            result_sequence.append(pred)
            detailed_sequence.append({
                "sample_name": item.get("sample_name"),
                "status": status,
                "collision_pred": pred,
            })

        return {
            "total_samples": len(sequence_results),
            "valid_samples": valid_samples,
            "failed_samples": failed_samples,
            "yes_count": yes_count,
            "no_count": no_count,
            "suspected_count": suspected_count,
            "result_sequence": result_sequence,
            "detailed_sequence": detailed_sequence,
        }

    def _cleanup_no_samples(self, task_dir: Path, sequence_results: list[dict[str, Any]]) -> dict[str, Any] | None:
        cleanup_cfg = self.ai_cfg.get("cleanup", {})
        enabled = bool(cleanup_cfg.get("delete_no_prediction_samples", True))
        if not enabled:
            return None

        deleted_dirs: list[str] = []
        missing_dirs: list[str] = []
        failed_dirs: list[dict[str, str]] = []
        for item in sequence_results:
            if item.get("collision_pred") != "否":
                continue
            sample_name = item.get("sample_name")
            if not sample_name:
                continue
            sample_dir = task_dir / str(sample_name)
            if not sample_dir.exists():
                missing_dirs.append(str(sample_dir))
                continue
            try:
                shutil.rmtree(sample_dir)
                deleted_dirs.append(str(sample_dir))
            except Exception as e:
                failed_dirs.append({"sample_dir": str(sample_dir), "error": str(e)})

        return {
            "enabled": True,
            "delete_target": "collision_pred=否",
            "deleted": len(failed_dirs) == 0,
            "deleted_sample_dirs": deleted_dirs,
            "missing_sample_dirs": missing_dirs,
            "failed_sample_dirs": failed_dirs,
        }
