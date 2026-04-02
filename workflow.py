# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from bag import DIBagDownloader
from frame import MultiFrameDecoder
from ai_runner import WorkflowAIProcessor


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
        if not self.output_root.exists():
            raise FileNotFoundError(f"输出目录不存在: {self.output_root}")
        row_dirs = [
            p for p in sorted(self.output_root.iterdir())
            if p.is_dir() and (p / "row_meta.json").exists()
        ]
        print(f"[INFO] 待解码 row 数量: {len(row_dirs)}")

        if self.ai_processor is not None:
            self.ai_processor.prepare()

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
                        "error": str(e),
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
        summary = {
            "output_root": str(self.output_root),
            "total_rows": len(all_rows_summary),
            "completed_rows": sum(1 for row in all_rows_summary if row.get("status") == "completed"),
            "failed_rows": sum(1 for row in all_rows_summary if row.get("status") != "completed"),
            "rows": all_rows_summary,
        }
        self._save_json(self.output_root / "workflow_summary.json", summary)
        return summary

    def run_ai_for_all_rows(self) -> dict[str, Any]:
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
        for row_dir in row_dirs:
            row_meta = self._load_json(row_dir / "row_meta.json")
            row_id = row_meta.get("row_id") or row_dir.name
            task_results: list[dict[str, Any]] = []
            for task in row_meta.get("tasks", []):
                task_id = task.get("task_id")
                if not task_id:
                    continue
                task_dir = row_dir / task_id
                task_meta_path = task_dir / "task_meta.json"
                if not task_meta_path.exists():
                    task_results.append({
                        "task_id": task_id,
                        "target_ts": task.get("target_ts"),
                        "status": "failed",
                        "error": f"missing task_meta: {task_meta_path}",
                        "task_dir": str(task_dir),
                    })
                    continue
                task_meta = self._load_json(task_meta_path)
                if not (task_dir / "manifest.json").exists():
                    task_meta.update({
                        "status": "failed",
                        "error": f"missing manifest: {task_dir / 'manifest.json'}",
                    })
                    task_results.append(task_meta)
                    self._write_task_result_to_disk(row_dir, task_meta)
                    continue
                print(f"[INFO] {row_id}/{task_id} 直接执行 AI")
                try:
                    ai_result = self.ai_processor.run_for_task_sequence(
                        row_meta=row_meta,
                        task_meta=task_meta,
                        task_dir=task_dir,
                    )
                    task_meta["ai"] = ai_result
                    task_meta["status"] = "completed" if ai_result.get("status") == "ok" else "ai_partial_failed"
                    task_meta.pop("error", None)
                except Exception as e:
                    task_meta["status"] = "failed"
                    task_meta["error"] = str(e)
                task_results.append(task_meta)
                self._write_task_result_to_disk(row_dir, task_meta)

            row_summary = self._finalize_row(row_dir, row_meta, task_results)
            row_summaries.append(self._build_workflow_row_summary(row_summary, row_dir))

        summary = {
            "output_root": str(self.output_root),
            "total_rows": len(row_summaries),
            "completed_rows": sum(1 for row in row_summaries if row.get("status") == "completed"),
            "failed_rows": sum(1 for row in row_summaries if row.get("status") != "completed"),
            "mode": "ai-only",
            "rows": row_summaries,
        }
        self._save_json(self.output_root / "workflow_summary.json", summary)
        return summary

    def run_pipeline(self) -> dict[str, Any]:
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
                        "error": str(e),
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
        summary = {
            "output_root": str(self.output_root),
            "total_rows": len(all_rows_summary),
            "completed_rows": sum(1 for row in all_rows_summary if row.get("status") == "completed"),
            "failed_rows": sum(1 for row in all_rows_summary if row.get("status") != "completed"),
            "rows": all_rows_summary,
        }
        self._save_json(self.output_root / "workflow_summary.json", summary)
        return summary

    # ---------------- worker ----------------

    def _process_task_worker(self, row_meta: dict[str, Any], task: dict[str, Any], run_ai: bool = True) -> dict[str, Any]:
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
        }

        if target_ts is None:
            task_meta["status"] = "missing_target_ts"
            return task_meta

        decoder_cfg = self._build_decoder_config_for_task(row_meta, float(target_ts), frames_out_dir)
        print(f"[INFO] {row_meta['row_id']}/{task_id} 开始解码, target_ts={float(target_ts):.9f}")

        try:
            decode_result = MultiFrameDecoder(decoder_cfg).run()
            task_meta.update({
                "status": "decoded",
                "manifest_path": decode_result["manifest_path"],
                "frames_root": decode_result["out_dir"],
                "decode_summary": decode_result.get("summary", {}),
            })

            if run_ai and self.ai_processor is not None:
                print(f"[INFO] {row_meta['row_id']}/{task_id} 开始逐帧 AI")
                ai_result = self.ai_processor.run_for_task_sequence(
                    row_meta=row_meta,
                    task_meta=task_meta,
                    task_dir=task_dir,
                )
                task_meta["ai"] = ai_result
                task_meta["status"] = "completed" if ai_result.get("status") == "ok" else "ai_partial_failed"
            else:
                task_meta["status"] = "completed"

        except Exception as e:
            task_meta["status"] = "failed"
            task_meta["error"] = str(e)
            print(f"[ERROR] {row_meta['row_id']}/{task_id} 失败: {e}")

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
            updated_tasks.append({
                "task_id": task.get("task_id"),
                "target_ts": task.get("target_ts"),
                "status": item.get("status"),
                "task_dir": item.get("task_dir"),
                "manifest_path": item.get("manifest_path"),
                "ai_status": (item.get("ai") or {}).get("status") if isinstance(item.get("ai"), dict) else None,
                "error": item.get("error"),
            })
        row_meta["tasks"] = updated_tasks

        total_tasks = len(results)
        ok_tasks = sum(1 for r in results if r.get("status") == "completed")
        failed_tasks = total_tasks - ok_tasks
        status = "completed" if total_tasks > 0 and failed_tasks == 0 else "partial_failed"

        row_summary = {
            "row_id": row_meta.get("row_id"),
            "excel_row": row_meta.get("excel_row"),
            "status": status,
            "total_tasks": total_tasks,
            "ok_tasks": ok_tasks,
            "failed_tasks": failed_tasks,
            "task_summaries": self._build_row_task_summaries(results),
        }

        cleanup_result = self._cleanup_bags_if_needed(row_dir, row_meta, row_summary)
        if cleanup_result is not None:
            row_summary["cleanup"] = cleanup_result
            row_meta["cleanup"] = cleanup_result

        row_meta["status"] = status
        row_meta["row_summary_path"] = str(row_dir / "row_summary.json")
        self._save_json(row_dir / "row_summary.json", row_summary)
        self._save_json(row_dir / "row_meta.json", row_meta)
        return row_summary

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
            "row_summary_path": str(row_dir / "row_summary.json"),
            "cleanup": row_summary.get("cleanup"),
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
    return parser.parse_args()


def main():
    args = parse_args()
    wf = RowWorkflow(args.config, force_delete_bags=args.force_delete_bags)
    try:
        enabled_modes = [args.download_only, args.decode_only, args.ai_only]
        if sum(1 for x in enabled_modes if x) > 1:
            raise ValueError("--download-only / --decode-only / --ai-only 只能选择一个")

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
    finally:
        wf.close()


if __name__ == "__main__":
    main()
