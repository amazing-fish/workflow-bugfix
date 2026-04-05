# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from tkinter import END, StringVar, Text, Tk
from tkinter import ttk

from preflight import run_preflight, format_preflight

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"

EXPLICIT_FAILURE_STATUSES = frozenset({
    "download_failed", "decode_failed", "ai_failed",
    "ai_partial_failed", "worker_failed", "ts_failed",
    "missing_target_ts",
})

PHASE_PATTERNS = [
    ("下载完成", "下载"), ("开始逐帧 AI", "AI"), ("开始解码", "解码"),
    ("待解码", "解码"), ("待执行仅 AI", "AI"), ("下载汇总", "下载完成"),
]

# -- Helpers ---------------------------------------------------------------

def _read_json(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

def _format_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def _is_failed(item: dict) -> bool:
    return (item.get("status") in EXPLICIT_FAILURE_STATUSES
            or item.get("failure_stage") is not None)

# -- Data collection -------------------------------------------------------

def _iter_row_dirs(output_root: Path):
    """Yield (row_dir, row_meta) for valid row directories."""
    if not output_root.exists():
        return
    for row_dir in sorted(output_root.iterdir()):
        if not row_dir.is_dir():
            continue
        row_meta = _read_json(row_dir / "row_meta.json")
        if row_meta:
            yield row_dir, row_meta

def collect_realtime_stats(output_root: Path) -> dict:
    """Real-time stats from disk."""
    stages = {"download": {"ok": 0, "fail": 0, "skipped": 0},
              "decode": {"ok": 0, "fail": 0, "skipped": 0},
              "ai": {"ok": 0, "fail": 0, "skipped": 0}}
    retry_stats = {"download": 0, "decode": 0, "ai": 0}
    row_count = task_count = completed_tasks = failed_tasks = pending_tasks = 0

    for row_dir, row_meta in _iter_row_dirs(output_root):
        row_count += 1
        dl_stats = row_meta.get("download_stats") or []
        retry_stats["download"] += sum(max(0, s.get("attempts", 1) - 1) for s in dl_stats)
        if row_meta.get("status") == "download_failed":
            stages["download"]["fail"] += 1
            continue
        stages["download"]["ok"] += 1
        for task_def in row_meta.get("tasks", []):
            task_id = task_def.get("task_id")
            if not task_id:
                continue
            task_count += 1
            task_meta = _read_json(row_dir / str(task_id) / "task_meta.json")
            if task_meta is None:
                pending_tasks += 1
                continue
            status = task_meta.get("status", "")
            if _is_failed({"status": status, "failure_stage": task_meta.get("failure_stage")}):
                failed_tasks += 1
            else:
                completed_tasks += 1
            if status == "decode_failed":
                stages["decode"]["fail"] += 1
            elif status in ("decoded", "decode_partial", "completed",
                            "ai_failed", "ai_partial_failed"):
                stages["decode"]["ok"] += 1
            if status in ("decoded", "decode_partial"):
                stages["ai"]["skipped"] += 1
            elif status == "completed":
                stages["ai"]["ok"] += 1
            elif status in ("ai_failed", "ai_partial_failed"):
                stages["ai"]["fail"] += 1
            ds = task_meta.get("decode_summary")
            if isinstance(ds, dict):
                retry_stats["decode"] += int(ds.get("decode_retries", 0))
            ai = task_meta.get("ai")
            if isinstance(ai, dict):
                agg = ai.get("aggregate")
                if isinstance(agg, dict):
                    retry_stats["ai"] += int(agg.get("total_retry_count", 0))

    has_failures = failed_tasks > 0 or stages["download"]["fail"] > 0
    return {"stages": stages, "retry_stats": retry_stats,
            "row_count": row_count, "task_count": task_count,
            "completed": completed_tasks, "failed": failed_tasks, "pending": pending_tasks,
            "has_failures": has_failures}

def collect_row_task_tree(output_root: Path) -> list[dict]:
    rows: list[dict] = []
    for row_dir, row_meta in _iter_row_dirs(output_root):
        row_id = row_meta.get("row_id", row_dir.name)
        row_summary = _read_json(row_dir / "row_summary.json")
        row_entry: dict = {
            "row_id": row_id, "row_dir": str(row_dir),
            "status": row_meta.get("status", "unknown"),
            "failure_stage": row_meta.get("failure_stage"),
            "reason": row_meta.get("reason"),
            "error": str(row_meta.get("error", ""))[:120],
            "elapsed_sec": None, "tasks": [],
        }
        if row_summary:
            row_entry["status"] = row_summary.get("status", row_entry["status"])
            row_entry["elapsed_sec"] = row_summary.get("elapsed_sec")
            row_entry["failure_stage"] = row_summary.get("primary_failure_stage") or row_entry["failure_stage"]
            row_entry["reason"] = row_summary.get("primary_failure_reason") or row_entry["reason"]
        if row_meta.get("status") == "download_failed":
            rows.append(row_entry)
            continue
        for task_def in row_meta.get("tasks", []):
            task_id = task_def.get("task_id")
            if not task_id:
                continue
            task_meta = _read_json(row_dir / str(task_id) / "task_meta.json")
            te: dict = {"task_id": str(task_id), "status": "pending",
                        "failure_stage": None, "reason": None, "error": "", "elapsed_sec": None}
            if task_meta:
                te["status"] = task_meta.get("status", "pending")
                te["failure_stage"] = task_meta.get("failure_stage")
                te["reason"] = task_meta.get("reason")
                te["error"] = str(task_meta.get("error", ""))[:120]
                te["elapsed_sec"] = task_meta.get("elapsed_sec")
            row_entry["tasks"].append(te)
        rows.append(row_entry)
    return rows

def collect_failure_list(output_root: Path) -> list[dict]:
    failures: list[dict] = []
    for row_dir, row_meta in _iter_row_dirs(output_root):
        row_id = row_meta.get("row_id", row_dir.name)
        if row_meta.get("status") == "download_failed":
            failures.append({"key": str(row_id),
                "failure_stage": row_meta.get("failure_stage", "download"),
                "reason": row_meta.get("reason", "download_error"),
                "error": str(row_meta.get("error", ""))[:120]})
            continue
        for task_def in row_meta.get("tasks", []):
            task_id = task_def.get("task_id")
            if not task_id:
                continue
            task_meta = _read_json(row_dir / str(task_id) / "task_meta.json")
            if not task_meta:
                continue
            if _is_failed(task_meta):
                failures.append({"key": f"{row_id}/{task_id}",
                    "failure_stage": task_meta.get("failure_stage", "unknown"),
                    "reason": task_meta.get("reason", "unknown"),
                    "error": str(task_meta.get("error", ""))[:120]})
    return failures

# -- GUI -------------------------------------------------------------------

class MonitorApp:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.cfg = load_config(config_path)
        output_root = (
            self.cfg.get("output", {}).get("root_dir")
            or self.cfg.get("excel", {}).get("output_dir")
            or "outputs"
        )
        self.output_root = Path(output_root)
        self.max_concurrency = int(self.cfg.get("pipeline", {}).get("max_concurrency", 1))
        self.proc: subprocess.Popen | None = None
        self.run_started_at: str | None = None
        self.run_start_mono: float | None = None
        self.current_phase: str = ""
        self._last_tree_snapshot: str = ""
        self._tree_refresh_counter: int = 0

        self.root = Tk()
        self.root.title("Workflow 可视化监控")
        self.root.geometry("960x620")

        self.status_text = StringVar(value="● 空闲")
        self.phase_text = StringVar(value="")
        self.elapsed_text = StringVar(value="00:00:00")
        self.auto_scroll = StringVar(value="on")
        self.filter_var = StringVar(value="全部")

        self._build_layout()
        self._refresh_loop()

    def _build_layout(self) -> None:
        style = ttk.Style()
        style.configure("Status.TLabel", font=("", 10))

        # Status bar
        status_bar = ttk.Frame(self.root)
        status_bar.pack(fill="x", padx=10, pady=(8, 2))
        ttk.Label(status_bar, textvariable=self.status_text, style="Status.TLabel").pack(side="left")
        ttk.Label(status_bar, textvariable=self.phase_text, style="Status.TLabel").pack(side="left", padx=12)
        ttk.Label(status_bar, textvariable=self.elapsed_text, style="Status.TLabel").pack(side="right")

        # Button bar
        btn_bar = ttk.Frame(self.root)
        btn_bar.pack(fill="x", padx=10, pady=4)
        modes = [
            ("全流程", "pipeline"), ("仅下载bag", "download"),
            ("解码(删bag)", "decode_cleanup"), ("直接AI", "ai"),
        ]
        for label, mode in modes:
            ttk.Button(btn_bar, text=label, command=lambda m=mode: self.start_mode(m)).pack(side="left", padx=3)
        ttk.Button(btn_bar, text="停止", command=self.stop_mode).pack(side="left", padx=3)
        ttk.Button(btn_bar, text="打开输出目录", command=self.open_output_dir).pack(side="left", padx=3)
        ttk.Button(btn_bar, text="刷新", command=self._manual_refresh).pack(side="right", padx=3)

        # Notebook
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(2, 8))

        self._build_overview_tab()
        self._build_row_task_tab()
        self._build_failure_tab()
        self._build_log_tab()

    def _build_overview_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="概览")

        cols = ("stage", "ok", "fail", "skipped", "rate")
        self.ov_tree = ttk.Treeview(tab, columns=cols, show="headings", height=4)
        for cid, text, w in [
            ("stage", "阶段", 80), ("ok", "成功", 70), ("fail", "失败", 70),
            ("skipped", "跳过", 70), ("rate", "成功率", 80),
        ]:
            self.ov_tree.heading(cid, text=text)
            self.ov_tree.column(cid, width=w, anchor="center")
        self.ov_tree.pack(fill="x", padx=8, pady=(8, 4))

        self.retry_label = ttk.Label(tab, text="重试统计: -")
        self.retry_label.pack(anchor="w", padx=8, pady=2)
        self.summary_label = ttk.Label(tab, text="汇总: -")
        self.summary_label.pack(anchor="w", padx=8, pady=2)

    def _build_row_task_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="任务列表")

        top = ttk.Frame(tab)
        top.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(top, text="筛选:").pack(side="left")
        cb = ttk.Combobox(top, textvariable=self.filter_var, state="readonly",
                          values=["全部", "仅失败", "仅成功", "仅待处理"], width=12)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda _: self._refresh_row_task_tree())

        cols = ("id", "status", "failure_stage", "reason", "elapsed")
        self.rt_tree = ttk.Treeview(tab, columns=cols, show="tree headings", height=16)
        self.rt_tree.heading("#0", text="")
        for cid, text, w in [
            ("id", "ID", 120), ("status", "状态", 100),
            ("failure_stage", "失败阶段", 90), ("reason", "原因", 140),
            ("elapsed", "耗时", 80),
        ]:
            self.rt_tree.heading(cid, text=text)
            self.rt_tree.column(cid, width=w, anchor="center")
        self.rt_tree.column("#0", width=30)

        vsb = ttk.Scrollbar(tab, orient="vertical", command=self.rt_tree.yview)
        self.rt_tree.configure(yscrollcommand=vsb.set)
        self.rt_tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=4)
        vsb.pack(side="right", fill="y", padx=(0, 8), pady=4)
        self.rt_tree.bind("<Double-1>", self._on_tree_double_click)

    def _build_failure_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="失败定位")

        cols = ("key", "failure_stage", "reason", "error")
        self.fail_tree = ttk.Treeview(tab, columns=cols, show="headings", height=14)
        for cid, text, w in [
            ("key", "Row/Task", 150), ("failure_stage", "失败阶段", 100),
            ("reason", "原因", 140), ("error", "错误摘要", 300),
        ]:
            self.fail_tree.heading(cid, text=text)
            self.fail_tree.column(cid, width=w, anchor="w")

        vsb = ttk.Scrollbar(tab, orient="vertical", command=self.fail_tree.yview)
        self.fail_tree.configure(yscrollcommand=vsb.set)
        self.fail_tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(8, 2))
        vsb.pack(side="right", fill="y", padx=(0, 8), pady=(8, 2))

        self.fail_agg_label = ttk.Label(tab, text="")
        self.fail_agg_label.pack(anchor="w", padx=8, pady=(0, 6), side="bottom")

    def _build_log_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="日志")

        toolbar = ttk.Frame(tab)
        toolbar.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Button(toolbar, text="清空", command=self.clear_logs).pack(side="left", padx=2)
        ttk.Button(toolbar, text="置底", command=self.scroll_to_bottom).pack(side="left", padx=2)
        ttk.Button(toolbar, text="自动滚动 开/关", command=self.toggle_auto_scroll).pack(side="left", padx=2)

        log_frame = ttk.Frame(tab)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        self.log = Text(log_frame, wrap="word", font=("Consolas", 9))
        self.log.pack(side="left", fill="both", expand=True)
        ysb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        ysb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=ysb.set)

    def _append_log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.insert(END, f"[{ts}] {msg}\n")
        if self.auto_scroll.get() == "on":
            self.log.see(END)

    def clear_logs(self) -> None:
        self.log.delete("1.0", END)

    def scroll_to_bottom(self) -> None:
        self.log.see(END)

    def toggle_auto_scroll(self) -> None:
        nxt = "off" if self.auto_scroll.get() == "on" else "on"
        self.auto_scroll.set(nxt)
        self._append_log(f"自动滚动已{'开启' if nxt == 'on' else '关闭'}")

    def _build_cmd(self, mode: str) -> list[str]:
        cmd = ["python", "workflow.py", "--config", str(self.config_path)]
        if mode == "download":
            cmd.append("--download-only")
        elif mode == "decode_cleanup":
            cmd.extend(["--decode-only", "--force-delete-bags"])
        elif mode == "ai":
            cmd.append("--ai-only")
        return cmd

    def start_mode(self, mode: str) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self._append_log("[WARN] 已有任务在运行，请先停止。")
            return
        _map = {"pipeline": "full", "download": "download-only",
                "decode_cleanup": "decode-only", "ai": "ai-only"}
        pf = run_preflight(self.config_path, mode=_map.get(mode, "full"))
        for line in format_preflight(pf).split("\n"):
            self._append_log(line)
        if not pf["passed"]:
            self._append_log("[FATAL] 预检未通过，请修复后重试。")
            return
        cmd = self._build_cmd(mode)
        self._append_log(f"[CMD] {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=False, bufsize=0,
        )
        self.proc = proc
        self.run_started_at = datetime.now(timezone.utc).isoformat()
        self.run_start_mono = time.monotonic()
        self.current_phase = ""
        self.status_text.set(f"● 运行中（{mode}）")
        threading.Thread(target=self._drain_output, args=(proc, mode), daemon=True).start()

    def stop_mode(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self._append_log("当前无运行中的任务。")
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)
        self._append_log("已终止任务。")
        self.status_text.set("● 已终止")

    def _drain_output(self, proc: subprocess.Popen, mode: str) -> None:
        if proc.stdout is not None:
            for raw in iter(proc.stdout.readline, b""):
                line = self._decode_line(raw).rstrip("\n")
                # Phase detection
                for pattern, phase in PHASE_PATTERNS:
                    if pattern in line:
                        self.current_phase = phase
                        break
                self.root.after(0, self._append_log, line)
        code = proc.wait()
        if self.proc is proc:
            label = "已完成" if code == 0 else f"异常退出({code})"
            self.root.after(0, self.status_text.set, f"● {label}（{mode}）")
            self.root.after(0, self._update_final_status)

    @staticmethod
    def _decode_line(raw: bytes) -> str:
        for enc in ("utf-8", "gbk"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _update_final_status(self) -> None:
        stats = collect_realtime_stats(self.output_root)
        if stats.get("has_failures"):
            total_fail = stats["failed"] + stats["stages"]["download"]["fail"]
            self.status_text.set(f"● 已完成（有 {total_fail} 个失败）")
        self.current_phase = ""

    def _should_use_workflow_summary(self) -> bool:
        if self.run_started_at is None:
            return False
        if self.proc is not None and self.proc.poll() is None:
            return False
        summary_path = self.output_root / "workflow_summary.json"
        summary = _read_json(summary_path)
        if not summary or not summary.get("started_at"):
            return False
        try:
            summary_start = datetime.fromisoformat(summary["started_at"])
            run_start = datetime.fromisoformat(self.run_started_at)
            return summary_start >= run_start
        except (ValueError, TypeError):
            return False

    def _refresh_loop(self) -> None:
        self._update_elapsed()
        self._update_overview()
        self._tree_refresh_counter += 1
        if self._tree_refresh_counter >= 3:
            self._tree_refresh_counter = 0
            self._refresh_row_task_tree()
            self._refresh_failure_tab()
        self.phase_text.set(f"阶段: {self.current_phase}" if self.current_phase else "")
        self.root.after(1000, self._refresh_loop)

    def _manual_refresh(self) -> None:
        self._update_overview()
        self._refresh_row_task_tree()
        self._refresh_failure_tab()

    def _update_elapsed(self) -> None:
        if self.run_start_mono is not None and self.proc is not None and self.proc.poll() is None:
            self.elapsed_text.set(_format_elapsed(time.monotonic() - self.run_start_mono))

    def _update_overview(self) -> None:
        stats = None
        if self._should_use_workflow_summary():
            summary = _read_json(self.output_root / "workflow_summary.json")
            if summary and "stage_stats" in summary:
                ss = summary["stage_stats"]
                stages = ss.get("stages", {})
                # Ensure all three stages exist with defaults
                for key in ("download", "decode", "ai"):
                    stages.setdefault(key, {"ok": 0, "fail": 0, "skipped": 0})
                retry_stats = ss.get("retry_stats", {"download": 0, "decode": 0, "ai": 0})
                row_count = summary.get("total_rows", 0)
                completed = summary.get("completed_rows", 0)
                failed = summary.get("failed_rows", 0)
                task_count = completed + failed
                pending = 0
                elapsed_sec = summary.get("elapsed_sec")
                if elapsed_sec is not None:
                    self.elapsed_text.set(_format_elapsed(elapsed_sec))
                has_failures = failed > 0 or stages.get("download", {}).get("fail", 0) > 0
                stats = {"stages": stages, "retry_stats": retry_stats,
                         "row_count": row_count, "task_count": task_count,
                         "completed": completed, "failed": failed, "pending": pending,
                         "has_failures": has_failures}
        if stats is None:
            stats = collect_realtime_stats(self.output_root)
        stages = stats["stages"]
        # Refresh stage table
        for item in self.ov_tree.get_children():
            self.ov_tree.delete(item)
        for name, label in [("download", "下载"), ("decode", "解码"), ("ai", "AI")]:
            s = stages[name]
            total = s["ok"] + s["fail"]
            rate = f"{s['ok']/total*100:.0f}%" if total > 0 else "-"
            self.ov_tree.insert("", END, values=(label, s["ok"], s["fail"], s.get("skipped", 0), rate))

        rs = stats["retry_stats"]
        self.retry_label.config(text=f"重试统计: 下载={rs['download']}  解码={rs['decode']}  AI={rs['ai']}")
        self.summary_label.config(
            text=f"汇总: row={stats['row_count']}  task={stats['task_count']}  "
                 f"完成={stats['completed']}  失败={stats['failed']}  待处理={stats['pending']}"
        )

    def _refresh_row_task_tree(self) -> None:
        rows = collect_row_task_tree(self.output_root)
        filt = self.filter_var.get()
        snapshot = json.dumps(rows, ensure_ascii=False, default=str)
        if snapshot == self._last_tree_snapshot and filt == getattr(self, "_last_filter", ""):
            return
        self._last_tree_snapshot = snapshot
        self._last_filter = filt

        self.rt_tree.delete(*self.rt_tree.get_children())
        for row in rows:
            if not self._row_matches_filter(row, filt):
                continue
            elapsed = _format_elapsed(row["elapsed_sec"]) if row.get("elapsed_sec") else ""
            rid = self.rt_tree.insert("", END, text="", values=(
                row["row_id"], row["status"],
                row.get("failure_stage") or "", row.get("reason") or "", elapsed,
            ), open=False)
            for t in row.get("tasks", []):
                if not self._task_matches_filter(t, filt):
                    continue
                te = _format_elapsed(t["elapsed_sec"]) if t.get("elapsed_sec") else ""
                self.rt_tree.insert(rid, END, text="", values=(
                    t["task_id"], t["status"],
                    t.get("failure_stage") or "", t.get("reason") or "", te,
                ))

    @staticmethod
    def _row_matches_filter(row: dict, filt: str) -> bool:
        if filt == "全部":
            return True
        if filt == "仅失败":
            return _is_failed(row) or any(_is_failed(t) for t in row.get("tasks", []))
        if filt == "仅成功":
            return not _is_failed(row)
        if filt == "仅待处理":
            return any(t.get("status") == "pending" for t in row.get("tasks", []))
        return True

    @staticmethod
    def _task_matches_filter(task: dict, filt: str) -> bool:
        if filt == "全部":
            return True
        if filt == "仅失败":
            return _is_failed(task)
        if filt == "仅成功":
            return not _is_failed(task) and task.get("status") != "pending"
        if filt == "仅待处理":
            return task.get("status") == "pending"
        return True

    def _refresh_failure_tab(self) -> None:
        failures = collect_failure_list(self.output_root)
        self.fail_tree.delete(*self.fail_tree.get_children())
        reason_counter: Counter = Counter()
        for f in failures:
            self.fail_tree.insert("", END, values=(
                f["key"], f["failure_stage"], f["reason"], f["error"],
            ))
            reason_counter[f.get("reason", "unknown")] += 1
        if reason_counter:
            agg = " | ".join(f"{k}: {v}" for k, v in reason_counter.most_common())
            self.fail_agg_label.config(text=f"失败分布: {agg}")
        else:
            self.fail_agg_label.config(text="无失败记录")

    def _on_tree_double_click(self, _event) -> None:
        sel = self.rt_tree.selection()
        if not sel:
            return
        item = self.rt_tree.item(sel[0])
        vals = item.get("values") or []
        if not vals:
            return
        parent = self.rt_tree.parent(sel[0])
        if parent:
            pvals = self.rt_tree.item(parent).get("values") or []
            rd = self._find_row_dir(str(pvals[0])) if pvals else None
            if rd:
                self._open_dir(str(rd / str(vals[0])))
        else:
            rd = self._find_row_dir(str(vals[0]))
            if rd:
                self._open_dir(str(rd))

    def _find_row_dir(self, row_id: str) -> Path | None:
        if not self.output_root.exists():
            return None
        for d in self.output_root.iterdir():
            if not d.is_dir():
                continue
            rm = _read_json(d / "row_meta.json")
            if rm and str(rm.get("row_id", d.name)) == row_id:
                return d
        return None

    def open_output_dir(self) -> None:
        self._open_dir(str(self.output_root))

    @staticmethod
    def _open_dir(path: str) -> None:
        if os.name == "nt":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])

    def run(self) -> None:
        self.root.mainloop()

def main() -> None:
    app = MonitorApp(DEFAULT_CONFIG)
    app.run()

if __name__ == "__main__":
    main()
