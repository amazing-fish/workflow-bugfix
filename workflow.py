# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from bag import DIBagDownloader
from frame import MultiFrameDecoder
from ai_runner import WorkflowAIProcessor
from preflight import run_preflight, format_preflight
from writeback import run_writeback


class RowWorkflow:
    """
    并发工作流。

    默认全流程：
    1. 主线程顺序下载每个 row 的 bag。
    2. 某个 row 下载完成后，立即把该 row 的 task 提交到全局线程池。
    3. 每个 task 内部顺序执行：解码 ->（可选）sample01..sample10 逐帧 AI。
    4. 全局 task 并发上限由 pipeline.max_concurrency 控制。
    5. 某个 row 全部 task 完成后，再根据配置删除该 row 的 bag。
    """

    def __init__(self, config_path: str | Path, force_delete_bags: bool = False):
        self.config_path = Path(config_path)
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.cfg = json.load(f)

        self.output_root = Path(
            self.cfg.get("output", {}).get("root_dir")
            or self.cfg.get("excel", {}).get("output_dir")
            or "outputs"
        )
        self.cleanup_cfg = self.cfg.get("cleanup", {})
        if force_delete_bags:
            self.cleanup_cfg["delete_bags_after_row"] = True
        self.pipeline_cfg = self.cfg.get("pipeline", {})
        self.max_concurrency = int(self.pipeline_cfg.get("max_concurrency", 3))
        self.ai_enabled = bool(self.cfg.get("ai", {}).get("enabled", False))
        self.ai_processor = WorkflowAIProcessor(self.cfg) if self.ai_enabled else None

    def close(self) -> None:
        if self.ai_processor is not None:
            self.ai_processor.close()

    # ---------------- entry ----------------

    def run_download(self) -> dict[str, Any]:
        downloader = DIBagDownloader(self.config_path)
        return downloader.run()

    def run_decode_for_row(self, row_dir: str | Path) -> dict[str, Any]:
        row_dir = Path(row_dir)
        row_meta = self._load_json(row_dir / "row_meta.json")
        results = []
        for task in row_meta.get("tasks", []):
            result = self._process_task_worker(row_meta, task, run_ai=False)
            results.append(result)
            self._write_task_result_to_disk(row_dir, result)
        row_summary = self._finalize_row(row_dir, row_meta, results)
        return {"row_dir": str(row_dir), "status": row_summary["status"], "summary": row_summary}

    def run_decode_for_all_rows(self) -> dict[str, Any]:
        wf_started = datetime.now(timezone.utc).isoformat()
        wf_t0 = time.monotonic()
        if not self.output_root.exists():
            raise FileNotFoundError(f"输出目录不存在: {self.output_root}")
        row_dirs = [
            p for p in sorted(self.output_root.iterdir())
            if p.is_dir() and (p / "row_meta.json").exists()
        ]
        print(f"[INFO] 待解码 row 数量: {len(row_dirs)}")

        all_rows_summary: list[dict[str, Any]] = []
        futures: dict[Any, tuple[str, dict[str, Any], dict[str, Any]]] = {}
        row_states: dict[str, dict[str, Any]] = {}

        with ThreadPoolExecutor(max_workers=self.max_concurrency, thread_name_prefix="decode-task") as executor:
            for row_dir in row_dirs:
                row_meta = self._load_json(row_dir / "row_meta.json")
                row_id = row_meta.get("row_id") or row_dir.name
                tasks = list(row_meta.get("tasks") or [])
                row_states[row_id] = {
                    "row_dir": row_dir,
                    "row_meta": row_meta,
                    "results": [],
                    "pending": len(tasks),
                    "submitted": len(tasks),
                }
                if not tasks:
                    summary = self._finalize_row(row_dir, row_meta, [])
                    all_rows_summary.append(self._build_workflow_row_summary(summary, row_dir))
                    continue
                for task in tasks:
                    future = executor.submit(self._process_task_worker, row_meta, task, False)
                    futures[future] = (row_id, row_meta, task)

            for future in as_completed(list(futures.keys())):
                row_id, row_meta, task = futures[future]
                row_state = row_states[row_id]
                row_dir = row_state["row_dir"]
                try:
                    result = future.result()
                except Exception as e:
                    result = {
                        "task_id": task.get("task_id"),
                        "target_ts": task.get("target_ts"),
                        "status": "worker_failed",
                        "failure_stage": "runtime",
                        "reason": str(e),
                        "error": str(e),
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "elapsed_sec": 0,
                    }
                row_state["results"].append(result)
                row_state["pending"] -= 1
                self._write_task_result_to_disk(row_dir, result)
                print(f"[INFO] {row_id}/{result.get('task_id')} 完成，剩余 pending={row_state['pending']}")
                if row_state["pending"] == 0:
                    summary = self._finalize_row(row_dir, row_state["row_meta"], row_state["results"])
                    all_rows_summary.append(self._build_workflow_row_summary(summary, row_dir))
                    print(f"[INFO] {row_id} 全部 task 完成")

        all_rows_summary.sort(key=lambda x: str(x.get("row_dir", "")))
        wf_finished = datetime.now(timezone.utc).isoformat()
        wf_elapsed = round(time.monotonic() - wf_t0, 2)
        summary = {
            "started_at": wf_started,
            "finished_at": wf_finished,
            "elapsed_sec": wf_elapsed,
            "output_root": str(self.output_root),
            "total_rows": len(all_rows_summary),
            "completed_rows": sum(1 for row in all_rows_summary if row.get("status") == "completed"),
            "failed_rows": sum(1 for row in all_rows_summary if row.get("status") != "completed"),
            "schema_version": "2.0",
            "run_mode": "full",
            "rows": all_rows_summary,
        }
        self._save_json(self.output_root / "workflow_summary.json", summary)
        return summary

    def run_ai_for_all_rows(self) -> dict[str, Any]:
        wf_started = datetime.now(timezone.utc).isoformat()
        wf_t0 = time.monotonic()
        if self.ai_processor is None:
            raise RuntimeError("config.ai.enabled=false，无法执行仅 AI 模式")
        if not self.output_root.exists():
            raise FileNotFoundError(f"输出目录不存在: {self.output_root}")

        row_dirs = [
            p for p in sorted(self.output_root.iterdir())
            if p.is_dir() and (p / "row_meta.json").exists()
        ]
        print(f"[INFO] 待执行仅 AI 的 row 数量: {len(row_dirs)}")
        self.ai_processor.prepare()

        row_summaries: list[dict[str, Any]] = []
        futures: dict[Any, tuple[str, dict[str, Any], dict[str, Any]]] = {}
        row_states: dict[str, dict[str, Any]] = {}

        with ThreadPoolExecutor(max_workers=self.max_concurrency, thread_name_prefix="ai-only-task") as executor:
            for row_dir in row_dirs:
                row_meta = self._load_json(row_dir / "row_meta.json")
                row_id = row_meta.get("row_id") or row_dir.name
                tasks = list(row_meta.get("tasks") or [])
                row_states[row_id] = {
                    "row_dir": row_dir,
                    "row_meta": row_meta,
                    "results": [],
                    "pending": 0,
                }
                for task in tasks:
                    task_id = task.get("task_id")
                    if not task_id:
                        continue
                    future = executor.submit(self._run_ai_only_task_worker, row_meta, task)
                    futures[future] = (row_id, row_meta, task)
                    row_states[row_id]["pending"] += 1
                if row_states[row_id]["pending"] == 0:
                    summary = self._finalize_row(row_dir, row_meta, [])
                    row_summaries.append(self._build_workflow_row_summary(summary, row_dir))

            for future in as_completed(list(futures.keys())):
                row_id, row_meta, task = futures[future]
                row_state = row_states[row_id]
                row_dir = row_state["row_dir"]
                try:
                    result = future.result()
                except Exception as e:
                    result = {
                        "task_id": task.get("task_id"),
                        "target_ts": task.get("target_ts"),
                        "status": "worker_failed",
                        "error": str(e),
                        "failure_stage": "runtime",
                        "reason": "worker_exception",
                        "task_dir": str(row_dir / str(task.get("task_id"))),
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "elapsed_sec": 0,
                    }
                row_state["results"].append(result)
                row_state["pending"] -= 1
                self._write_task_result_to_disk(row_dir, result)
                print(f"[INFO] {row_id}/{result.get('task_id')} AI-only 完成，剩余 pending={row_state['pending']}")

                if row_state["pending"] == 0:
                    summary = self._finalize_row(row_dir, row_meta, row_state["results"])
                    row_summaries.append(self._build_workflow_row_summary(summary, row_dir))
                    print(f"[INFO] {row_id} AI-only 全部 task 完成")

        row_summaries.sort(key=lambda x: str(x.get("row_dir", "")))
        wf_finished = datetime.now(timezone.utc).isoformat()
        wf_elapsed = round(time.monotonic() - wf_t0, 2)
        summary = {
            "started_at": wf_started,
            "finished_at": wf_finished,
            "elapsed_sec": wf_elapsed,
            "output_root": str(self.output_root),
            "total_rows": len(row_summaries),
            "completed_rows": sum(1 for row in row_summaries if row.get("status") == "completed"),
            "failed_rows": sum(1 for row in row_summaries if row.get("status") != "completed"),
            "schema_version": "2.0",
            "run_mode": "ai-only",
            "rows": row_summaries,
        }
        self._save_json(self.output_root / "workflow_summary.json", summary)
        return summary

    def run_pipeline(self) -> dict[str, Any]:
        wf_started = datetime.now(timezone.utc).isoformat()
        wf_t0 = time.monotonic()
        downloader = DIBagDownloader(self.config_path)
        if self.ai_processor is not None:
            self.ai_processor.prepare()

        all_rows_summary: list[dict[str, Any]] = []
        futures: dict[Any, tuple[str, dict[str, Any], dict[str, Any]]] = {}
        row_states: dict[str, dict[str, Any]] = {}

        with ThreadPoolExecutor(max_workers=self.max_concurrency, thread_name_prefix="row-task") as executor:
            for row_result in downloader.iter_download_rows():
                row_id = row_result.get("row_id")
                if row_result.get("status") != "downloaded":
                    all_rows_summary.append(row_result)
                    continue

                row_dir = Path(row_result["row_dir"])
                row_meta = self._load_json(row_dir / "row_meta.json")
                tasks = list(row_meta.get("tasks") or [])
                row_states[row_id] = {
                    "row_dir": row_dir,
                    "row_meta": row_meta,
                    "results": [],
                    "pending": len(tasks),
                    "submitted": len(tasks),
                }

                if not tasks:
                    summary = self._finalize_row(row_dir, row_meta, [])
                    all_rows_summary.append(self._build_workflow_row_summary(summary, row_dir))
                    continue

                print(f"[INFO] {row_id} 下载完成，提交 {len(tasks)} 个 task 到线程池")
                for task in tasks:
                    future = executor.submit(self._process_task_worker, row_meta, task, True)
                    futures[future] = (row_id, row_meta, task)

            for future in as_completed(list(futures.keys())):
                row_id, row_meta, task = futures[future]
                row_state = row_states[row_id]
                row_dir = row_state["row_dir"]
                try:
                    result = future.result()
                except Exception as e:
                    result = {
                        "task_id": task.get("task_id"),
                        "target_ts": task.get("target_ts"),
                        "status": "worker_failed",
                        "failure_stage": "runtime",
                        "reason": str(e),
                        "error": str(e),
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "elapsed_sec": 0,
                    }
                row_state["results"].append(result)
                row_state["pending"] -= 1
                self._write_task_result_to_disk(row_dir, result)
                print(f"[INFO] {row_id}/{result.get('task_id')} 完成，剩余 pending={row_state['pending']}")

                if row_state["pending"] == 0:
                    summary = self._finalize_row(row_dir, row_state["row_meta"], row_state["results"])
                    all_rows_summary.append(self._build_workflow_row_summary(summary, row_dir))
                    print(f"[INFO] {row_id} 全部 task 完成")

        all_rows_summary.sort(key=lambda x: str(x.get("row_dir", "")))
        wf_finished = datetime.now(timezone.utc).isoformat()
        wf_elapsed = round(time.monotonic() - wf_t0, 2)
        summary = {
            "started_at": wf_started,
            "finished_at": wf_finished,
            "elapsed_sec": wf_elapsed,
            "output_root": str(self.output_root),
            "total_rows": len(all_rows_summary),
            "completed_rows": sum(1 for row in all_rows_summary if row.get("status") == "completed"),
            "failed_rows": sum(1 for row in all_rows_summary if row.get("status") != "completed"),
            "schema_version": "2.0",
            "run_mode": "decode-only",
            "rows": all_rows_summary,
        }
        self._save_json(self.output_root / "workflow_summary.json", summary)
        return summary

    # ---------------- worker ----------------

    def _process_task_worker(self, row_meta: dict[str, Any], task: dict[str, Any], run_ai: bool = True) -> dict[str, Any]:
        _t_start = time.monotonic()
        _t_start_wall = datetime.now(timezone.utc).isoformat()
        task_id = task["task_id"]
        target_ts = task.get("target_ts")
        row_dir = self.output_root / row_meta["row_id"]
        task_dir = row_dir / task_id
        frames_out_dir = task_dir
        task_dir.mkdir(parents=True, exist_ok=True)

        task_meta: dict[str, Any] = {
            "task_id": task_id,
            "target_ts": float(target_ts) if target_ts is not None else None,
            "status": "pending",
            "task_dir": str(task_dir),
            "started_at": _t_start_wall,
        }

        if target_ts is None:
            task_meta["status"] = "ts_failed"
            task_meta["failure_stage"] = "timestamp"
            task_meta["reason"] = "missing_target_ts"
            task_meta["finished_at"] = datetime.now(timezone.utc).isoformat()
            task_meta["elapsed_sec"] = round(time.monotonic() - _t_start, 2)
            return task_meta

        decoder_cfg = self._build_decoder_config_for_task(row_meta, float(target_ts), frames_out_dir)
        print(f"[INFO] {row_meta['row_id']}/{task_id} 开始解码, target_ts={float(target_ts):.9f}")

        try:
            decode_result = MultiFrameDecoder(decoder_cfg).run()
            ds = decode_result.get("summary", {})
            ok_frames = ds.get("ok_frames", 0)
            expected_frames = ds.get("expected_frames", ds.get("total_frames", 0))
            if ok_frames == 0:
                decode_status = "decode_failed"
            elif ok_frames < expected_frames:
                decode_status = "decode_partial"
            else:
                decode_status = "decoded"

            task_meta.update({
                "status": decode_status,
                "manifest_path": decode_result["manifest_path"],
                "frames_root": decode_result["out_dir"],
                "decode_summary": ds,
            })
            if decode_status == "decode_failed":
                task_meta["failure_stage"] = "decode"
                task_meta["reason"] = "zero_frames_decoded"

            if run_ai and self.ai_processor is not None and decode_status != "decode_failed":
                print(f"[INFO] {row_meta['row_id']}/{task_id} 开始逐帧 AI")
                ai_result = self.ai_processor.run_for_task_sequence(
                    row_meta=row_meta,
                    task_meta=task_meta,
                    task_dir=task_dir,
                )
                task_meta["ai"] = self._compact_ai_result(ai_result)
                if ai_result.get("status") == "ok":
                    task_meta["status"] = "completed"
                else:
                    task_meta["status"] = "ai_partial_failed"
                    task_meta["failure_stage"] = "ai"
                    task_meta["reason"] = "ai_partial_failed"

        except Exception as e:
            task_meta["status"] = "ai_failed" if "manifest_path" in task_meta else "decode_failed"
            task_meta["error"] = str(e)
            task_meta["failure_stage"] = "ai" if "manifest_path" in task_meta else "decode"
            task_meta["reason"] = "inference_failed" if "manifest_path" in task_meta else "decode_error"
            print(f"[ERROR] {row_meta['row_id']}/{task_id} 失败: {e}")

        task_meta["finished_at"] = datetime.now(timezone.utc).isoformat()
        task_meta["elapsed_sec"] = round(time.monotonic() - _t_start, 2)
        return task_meta

    def _run_ai_only_task_worker(self, row_meta: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        _t_start = time.monotonic()
        task_id = task.get("task_id")
        row_dir = self.output_root / row_meta["row_id"]
        task_dir = row_dir / str(task_id)
        task_meta_path = task_dir / "task_meta.json"

        if not task_id:
            return {
                "task_id": None,
                "target_ts": task.get("target_ts"),
                "status": "ai_failed",
                "error": "missing task_id",
                "failure_stage": "ai",
                "reason": "missing_task_id",
                "task_dir": str(task_dir),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_sec": 0,
            }
        if not task_meta_path.exists():
            return {
                "task_id": task_id,
                "target_ts": task.get("target_ts"),
                "status": "ai_failed",
                "error": f"missing task_meta: {task_meta_path}",
                "failure_stage": "ai",
                "reason": "missing_task_meta",
                "task_dir": str(task_dir),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_sec": 0,
            }

        task_meta = self._load_json(task_meta_path)
        task_meta["started_at"] = datetime.now(timezone.utc).isoformat()
        manifest_path = task_dir / "manifest.json"
        if not manifest_path.exists():
            task_meta.update({
                "status": "ai_failed",
                "error": f"missing manifest: {manifest_path}",
                "failure_stage": "ai",
                "reason": "missing_manifest",
            })
            task_meta["finished_at"] = datetime.now(timezone.utc).isoformat()
            task_meta["elapsed_sec"] = round(time.monotonic() - _t_start, 2)
            return task_meta

        row_id = row_meta.get("row_id") or row_dir.name
        print(f"[INFO] {row_id}/{task_id} 直接执行 AI")
        try:
            ai_result = self.ai_processor.run_for_task_sequence(
                row_meta=row_meta,
                task_meta=task_meta,
                task_dir=task_dir,
            )
            task_meta["ai"] = self._compact_ai_result(ai_result)
            task_meta["status"] = "completed" if ai_result.get("status") == "ok" else "ai_partial_failed"
            task_meta.pop("error", None)
        except Exception as e:
            task_meta["status"] = "ai_failed"
            task_meta["error"] = str(e)
            task_meta["failure_stage"] = "ai"
            task_meta["reason"] = str(e)[:200]
        task_meta["finished_at"] = datetime.now(timezone.utc).isoformat()
        task_meta["elapsed_sec"] = round(time.monotonic() - _t_start, 2)
        return task_meta

    # ---------------- helpers ----------------

    def _build_decoder_config_for_task(self, row_meta: dict[str, Any], target_ts: float, out_dir: Path) -> dict[str, Any]:
        downloaded = row_meta.get("downloaded") or {}
        topics = self.cfg.get("topics", [])
        bag_paths = []
        missing_topics = []
        for topic in topics:
            p = downloaded.get(topic)
            if not p:
                missing_topics.append(topic)
                continue
            bag_paths.append(p)
        if not bag_paths:
            raise RuntimeError(f"row={row_meta.get('row_id')} 没有可用 bag_paths")
        if missing_topics:
            print(f"[WARN] row={row_meta.get('row_id')} 缺少部分 topics: {missing_topics}")

        sampling_cfg = self.cfg.get("sampling", {})
        decoder_cfg = self.cfg.get("decoder", {})
        match_cfg = self.cfg.get("match") or {
            "topic_keywords": decoder_cfg.get("topic_keywords", ["camera", "encoded", "h265", "fisheye"]),
            "payload_fields": decoder_cfg.get("payload_fields", ["data", "raw_data", "payload"]),
        }

        return {
            "input": {"bag_paths": bag_paths, "target_ts": float(target_ts)},
            "output": {"out_dir": str(out_dir)},
            "sampling": {
                "before_frames": int(sampling_cfg.get("before_frames", 4)),
                "after_frames": int(sampling_cfg.get("after_frames", 5)),
            },
            "decoder": {
                "ffmpeg_path": decoder_cfg.get("ffmpeg_path", "ffmpeg"),
                "loglevel": decoder_cfg.get("loglevel", "error"),
                "image_ext": decoder_cfg.get("image_ext", "png"),
                "context_before_packets": int(decoder_cfg.get("context_before_packets", 12)),
                "context_after_packets": int(decoder_cfg.get("context_after_packets", 0)),
            },
            "match": {
                "topic_keywords": match_cfg.get("topic_keywords", ["camera", "encoded", "h265", "fisheye"]),
                "payload_fields": match_cfg.get("payload_fields", ["data", "raw_data", "payload"]),
            },
        }

    @staticmethod
    def _compact_ai_result(ai_result: dict[str, Any]) -> dict[str, Any]:
        result_dir = ai_result.get("result_dir")
        summary_path = str(Path(result_dir) / "ai_result_summary.json") if result_dir else None
        return {
            "status": ai_result.get("status"),
            "elapsed_sec": ai_result.get("elapsed_sec"),
            "sample_count": ai_result.get("sample_count"),
            "aggregate": ai_result.get("aggregate"),
            "analysis": ai_result.get("analysis"),
            "retained_samples": ai_result.get("retained_samples"),
            "cleanup": ai_result.get("cleanup"),
            "ai_result_summary_path": summary_path,
        }

    def _write_task_result_to_disk(self, row_dir: Path, result: dict[str, Any]) -> None:
        task_id = result.get("task_id") or "unknown"
        task_dir = row_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        self._save_json(task_dir / "task_meta.json", result)

    def _finalize_row(self, row_dir: Path, row_meta: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
        tasks_by_id = {item.get("task_id"): item for item in results}
        updated_tasks = []
        for task in row_meta.get("tasks", []):
            item = tasks_by_id.get(task.get("task_id"))
            if item is None:
                updated_tasks.append(task)
                continue
            merged = dict(task)
            merged.update(item)
            merged["ai_status"] = (item.get("ai") or {}).get("status") if isinstance(item.get("ai"), dict) else None
            merged.pop("ai", None)
            updated_tasks.append(merged)
        row_meta["tasks"] = updated_tasks

        total_tasks = len(results)
        ok_tasks = sum(1 for r in results if r.get("status") in ("completed", "decoded", "decode_partial"))
        failed_tasks = total_tasks - ok_tasks
        status = "completed" if total_tasks > 0 and failed_tasks == 0 else ("failed" if ok_tasks == 0 else "partial_failed")

        # Row 级失败归因
        STAGE_PRIORITY = {"download": 0, "timestamp": 1, "decode": 2, "upload": 3, "inference": 4, "ai": 5, "runtime": 6, "cleanup": 7}
        failure_reasons = []
        stage_counts = {}
        for r in results:
            fs = r.get("failure_stage")
            reason = r.get("reason")
            if fs:
                stage_counts[fs] = stage_counts.get(fs, 0) + 1
                if reason and reason not in failure_reasons:
                    failure_reasons.append(reason)
        primary_failure_stage = min(stage_counts, key=lambda s: STAGE_PRIORITY.get(s, 99)) if stage_counts else None
        # 从最高优先级 stage 中选出现次数最多的 reason
        primary_failure_reason = None
        if primary_failure_stage:
            stage_reasons: dict[str, int] = {}
            for r in results:
                if r.get("failure_stage") == primary_failure_stage and r.get("reason"):
                    rn = r["reason"]
                    stage_reasons[rn] = stage_reasons.get(rn, 0) + 1
            if stage_reasons:
                primary_failure_reason = max(stage_reasons, key=stage_reasons.get)
        row_analysis = self._build_row_analysis(row_meta, results)

        stage_stats = self._compute_stage_stats(results)
        elapsed_list = [r.get("elapsed_sec") for r in results if r.get("elapsed_sec") is not None]

        row_summary = {
            "row_id": row_meta.get("row_id"),
            "excel_row": row_meta.get("excel_row"),
            "status": status,
            "primary_failure_stage": primary_failure_stage,
            "primary_failure_reason": primary_failure_reason,
            "failure_reasons": failure_reasons,
            "total_tasks": total_tasks,
            "ok_tasks": ok_tasks,
            "failed_tasks": failed_tasks,
            "stage_stats": stage_stats,
            "timing": {
                "avg_task_sec": round(sum(elapsed_list) / len(elapsed_list), 2) if elapsed_list else None,
                "max_task_sec": round(max(elapsed_list), 2) if elapsed_list else None,
                "min_task_sec": round(min(elapsed_list), 2) if elapsed_list else None,
            },
            "analysis": row_analysis,
            "task_summaries": self._build_row_task_summaries(results),
        }

        cleanup_result = self._cleanup_bags_if_needed(row_dir, row_meta, row_summary)
        if cleanup_result is not None:
            row_summary["cleanup"] = cleanup_result
            row_meta["cleanup"] = cleanup_result

        row_meta["status"] = status
        row_meta["analysis"] = row_analysis
        row_meta["row_summary_path"] = str(row_dir / "row_summary.json")
        self._save_json(row_dir / "row_summary.json", row_summary)
        self._save_json(row_dir / "row_meta.json", row_meta)
        print(f"[INFO] {row_analysis['count_line']}")
        print(f"[INFO] {row_analysis['row_key']} 保留样本: {row_analysis['retained_line']}")
        return row_summary

    @staticmethod
    def _compute_stage_stats(results: list[dict[str, Any]]) -> dict[str, Any]:
        stages = {
            "timestamp": {"ok": 0, "fail": 0},
            "download": {"ok": 0, "fail": 0},
            "decode": {"ok": 0, "fail": 0},
            "ai": {"ok": 0, "fail": 0, "skipped": 0},
        }
        fail_stage_dist: list[str] = []
        for r in results:
            status = r.get("status", "")
            ai_status = (r.get("ai") or {}).get("status") if isinstance(r.get("ai"), dict) else r.get("ai_status")
            if status == "download_failed":
                stages["download"]["fail"] += 1
                fail_stage_dist.append("download")
                continue
            stages["download"]["ok"] += 1
            if status in ("ts_failed", "missing_target_ts"):
                stages["timestamp"]["fail"] += 1
                fail_stage_dist.append("timestamp")
                continue
            stages["timestamp"]["ok"] += 1
            if status == "decode_failed":
                stages["decode"]["fail"] += 1
                fail_stage_dist.append("decode")
                continue
            if status == "worker_failed":
                stages["runtime"]["fail"] += 1
                fail_stage_dist.append("runtime")
                continue
            if status in ("decoded", "decode_partial"):
                stages["decode"]["ok"] += 1
            elif status == "completed":
                stages["decode"]["ok"] += 1
            if status in ("decoded", "decode_partial"):
                stages["ai"]["skipped"] += 1
                continue
            if ai_status in ("completed", "ok"):
                stages["ai"]["ok"] += 1
            elif ai_status in ("failed", "partial_failed", "ai_failed"):
                stages["ai"]["fail"] += 1
                fail_stage_dist.append("ai")
            elif status == "ai_failed":
                stages["ai"]["fail"] += 1
                fail_stage_dist.append("ai")
            elif ai_status is None:
                stages["ai"]["skipped"] += 1
        for key in stages:
            total = stages[key]["ok"] + stages[key]["fail"] + stages[key].get("skipped", 0)
            stages[key]["total"] = total
            stages[key]["success_rate"] = round(stages[key]["ok"] / total, 2) if total > 0 else None
        return {"stages": stages, "fail_stage_distribution": fail_stage_dist}

    @staticmethod
    def _build_row_task_summaries(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        summaries = []
        for item in sorted(results, key=lambda x: str(x.get("task_id", ""))):
            task_id = item.get("task_id")
            task_dir = item.get("task_dir")
            summaries.append({
                "task_id": task_id,
                "target_ts": item.get("target_ts"),
                "status": item.get("status"),
                "ai_status": (item.get("ai") or {}).get("status") if isinstance(item.get("ai"), dict) else None,
                "error": item.get("error"),
                "failure_stage": item.get("failure_stage"),
                "reason": item.get("reason"),
                "task_summary_path": str(Path(task_dir) / "task_meta.json") if task_dir else None,
            })
        return summaries

    @staticmethod
    def _build_workflow_row_summary(row_summary: dict[str, Any], row_dir: Path) -> dict[str, Any]:
        return {
            "row_id": row_summary.get("row_id") or row_dir.name,
            "row_dir": str(row_dir),
            "status": row_summary.get("status"),
            "total_tasks": row_summary.get("total_tasks"),
            "ok_tasks": row_summary.get("ok_tasks"),
            "failed_tasks": row_summary.get("failed_tasks"),
            "analysis": row_summary.get("analysis"),
            "row_summary_path": str(row_dir / "row_summary.json"),
            "cleanup": row_summary.get("cleanup"),
            "stage_stats": row_summary.get("stage_stats"),
            "timing": row_summary.get("timing"),
            "primary_failure_stage": row_summary.get("primary_failure_stage"),
            "primary_failure_reason": row_summary.get("primary_failure_reason"),
            "primary_failure_stage": row_summary.get("primary_failure_stage"),
            "primary_failure_reason": row_summary.get("primary_failure_reason"),
            "primary_failure_stage": row_summary.get("primary_failure_stage"),
            "primary_failure_reason": row_summary.get("primary_failure_reason"),
            "failure_reasons": row_summary.get("failure_reasons"),
        }

    @staticmethod
    def _build_row_analysis(row_meta: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
        row_key = str(row_meta.get("row_id") or "unknown_row")
        no_count = 0
        suspected_count = 0
        yes_count = 0
        retained_pairs: list[str] = []
        task_lines: list[str] = []

        for item in sorted(results, key=lambda x: str(x.get("task_id", ""))):
            task_id = str(item.get("task_id") or "unknown_task")
            ai_summary = item.get("ai") if isinstance(item.get("ai"), dict) else {}
            aggregate = ai_summary.get("aggregate") if isinstance(ai_summary.get("aggregate"), dict) else {}
            no_count += int(aggregate.get("no_count", 0))
            suspected_count += int(aggregate.get("suspected_count", 0))
            yes_count += int(aggregate.get("yes_count", 0))

            analysis = ai_summary.get("analysis") if isinstance(ai_summary.get("analysis"), dict) else {}
            task_count_line = analysis.get("count_line")
            if task_count_line:
                task_lines.append(str(task_count_line))

            retained_samples = ai_summary.get("retained_samples")
            if isinstance(retained_samples, list):
                for sample in retained_samples:
                    if not isinstance(sample, dict):
                        continue
                    sample_name = sample.get("sample_name")
                    result = sample.get("result")
                    if not sample_name or not result:
                        continue
                    retained_pairs.append(f"{task_id}/{sample_name}:{result}")

        count_line = f"{row_key}: [no:{no_count},suspected:{suspected_count},yes:{yes_count}]"
        retained_line = f"[{','.join(retained_pairs)}]" if retained_pairs else "[]"
        return {
            "row_key": row_key,
            "counts": {
                "no": no_count,
                "suspected": suspected_count,
                "yes": yes_count,
            },
            "count_line": count_line,
            "retained_line": retained_line,
            "task_count_lines": task_lines,
        }

    def _cleanup_bags_if_needed(self, row_dir: Path, row_meta: dict[str, Any], row_summary: dict[str, Any]) -> dict[str, Any] | None:
        enabled = bool(self.cleanup_cfg.get("delete_bags_after_row", False))
        only_if_all_ok = bool(self.cleanup_cfg.get("delete_only_if_all_tasks_succeeded", True))
        if not enabled:
            return None
        if only_if_all_ok and row_summary.get("failed_tasks", 0) > 0:
            return {"enabled": True, "deleted": False, "reason": "failed_tasks_present"}

        downloaded = row_meta.get("downloaded") or {}
        deleted_files = []
        missing_files = []
        failed_files = []
        for _, bag_path_str in downloaded.items():
            bag_path = Path(bag_path_str)
            if not bag_path.exists():
                missing_files.append(str(bag_path))
                continue
            try:
                bag_path.unlink()
                deleted_files.append(str(bag_path))
            except Exception as e:
                failed_files.append({"path": str(bag_path), "error": str(e)})

        return {
            "enabled": True,
            "deleted": len(failed_files) == 0,
            "deleted_files": deleted_files,
            "missing_files": missing_files,
            "failed_files": failed_files,
        }

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _save_json(path: Path, data: dict[str, Any]):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def parse_args():
    parser = argparse.ArgumentParser(description="DriveInsight 行级下载 + 多时间点解码 + 逐帧 AI 并发工作流")
    parser.add_argument("--config", default="config.json", help="config.json 路径")
    parser.add_argument("--download-only", action="store_true", help="仅执行下载")
    parser.add_argument("--decode-only", action="store_true", help="仅对已存在 row 执行解码（不调用 AI）")
    parser.add_argument("--ai-only", action="store_true", help="仅执行 AI（基于已存在 manifest/图片）")
    parser.add_argument("--force-delete-bags", action="store_true", help="执行完 row 后强制删除 bag（覆盖 config.cleanup.delete_bags_after_row）")
    parser.add_argument("--row-dir", help="仅对单个 row_dir 执行解码 + AI")
    parser.add_argument("--writeback-only", action="store_true", help="仅执行 Excel 回填（基于已有 row_summary）")
    return parser.parse_args()


def main():
    args = parse_args()
    preflight_mode = "full"
    if args.download_only:
        preflight_mode = "download-only"
    elif args.decode_only:
        preflight_mode = "decode-only"
    elif args.ai_only:
        preflight_mode = "ai-only"
    elif getattr(args, "writeback_only", False):
        preflight_mode = "writeback-only"
    preflight_result = run_preflight(args.config, mode=preflight_mode)
    print(format_preflight(preflight_result))
    if not preflight_result["passed"]:
        print("[FATAL] 预检未通过，请修复上述问题后重试。")
        raise SystemExit(1)

    wf = RowWorkflow(args.config, force_delete_bags=args.force_delete_bags)
    try:
        enabled_modes = [args.download_only, args.decode_only, args.ai_only, args.writeback_only]
        if sum(1 for x in enabled_modes if x) > 1:
            raise ValueError("--download-only / --decode-only / --ai-only 只能选择一个")

        if args.writeback_only:
            run_writeback(args.config)
            return

        if args.download_only:
            wf.run_download()
            return

        if args.decode_only:
            if args.row_dir:
                wf.run_decode_for_row(args.row_dir)
            else:
                wf.run_decode_for_all_rows()
            return

        if args.ai_only:
            wf.run_ai_for_all_rows()
            return

        wf.run_pipeline()
        try:
            run_writeback(args.config)
        except Exception as e:
            print(f"[WARN] Excel 回填失败: {e}")
    finally:
        wf.close()


if __name__ == "__main__":
    main()
