# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, Button, Frame, Label, Scrollbar, StringVar, Text, Tk


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def collect_task_stats(output_root: Path) -> dict:
    stats = {"rows": 0, "total_tasks": 0, "completed": 0, "failed": 0, "pending": 0}
    if not output_root.exists():
        return stats
    for row_dir in sorted(output_root.iterdir()):
        if not row_dir.is_dir():
            continue
        row_meta = row_dir / "row_meta.json"
        if not row_meta.exists():
            continue
        stats["rows"] += 1
        try:
            with open(row_meta, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue
        for task in meta.get("tasks", []):
            stats["total_tasks"] += 1
            status = task.get("status")
            if status == "completed":
                stats["completed"] += 1
            elif status in {"failed", "worker_failed", "ai_partial_failed"}:
                stats["failed"] += 1
            else:
                stats["pending"] += 1
    return stats


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

        self.root = Tk()
        self.root.title("Workflow 可视化监控")
        self.root.geometry("920x560")

        self.status_text = StringVar(value="空闲")
        self.stats_text = StringVar(value="-")
        self.thread_text = StringVar(value="-")

        self._build_layout()
        self._refresh_loop()

    def _build_layout(self) -> None:
        self.root.configure(bg="#f6f8fb")

        top = Frame(self.root)
        top.pack(fill=BOTH, padx=12, pady=12)

        Label(top, text="运行状态：").pack(side=LEFT)
        Label(top, textvariable=self.status_text).pack(side=LEFT)
        Label(top, text=" | 任务统计：").pack(side=LEFT)
        Label(top, textvariable=self.stats_text).pack(side=LEFT)
        Label(top, text=" | 线程情况：").pack(side=LEFT)
        Label(top, textvariable=self.thread_text).pack(side=LEFT)

        btns = Frame(self.root)
        btns.pack(fill=BOTH, padx=12, pady=8)

        Button(btns, text="开始：全流程", command=lambda: self.start_mode("pipeline")).pack(side=LEFT, padx=6)
        Button(btns, text="开始：仅下载bag", command=lambda: self.start_mode("download")).pack(side=LEFT, padx=6)
        Button(btns, text="开始：解码图片（删除bag）", command=lambda: self.start_mode("decode_cleanup")).pack(side=LEFT, padx=6)
        Button(btns, text="开始：直接调用AI", command=lambda: self.start_mode("ai")).pack(side=LEFT, padx=6)
        Button(btns, text="清空日志", command=self.clear_logs).pack(side=RIGHT, padx=6)
        Button(btns, text="停止当前任务", command=self.stop_mode).pack(side=RIGHT, padx=6)

        log_tools = Frame(self.root)
        log_tools.pack(fill=X, padx=12, pady=(0, 2))

        self.auto_scroll = StringVar(value="on")
        Button(log_tools, text="日志置底", command=self.scroll_to_bottom).pack(side=LEFT, padx=(0, 6))
        Button(log_tools, text="自动滚动：开/关", command=self.toggle_auto_scroll).pack(side=LEFT)

        log_frame = Frame(self.root)
        log_frame.pack(fill=BOTH, expand=True, padx=12, pady=(2, 8))
        self.log = Text(log_frame, wrap="word")
        self.log.pack(side=LEFT, fill=BOTH, expand=True)

        y_scroll = Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        y_scroll.pack(side=RIGHT, fill=Y)
        self.log.configure(yscrollcommand=y_scroll.set)

    def _append_log(self, msg: str) -> None:
        self.log.insert(END, self._with_timestamp(msg) + "\n")
        if self.auto_scroll.get() == "on":
            self.log.see(END)

    @staticmethod
    def _with_timestamp(msg: str) -> str:
        timestamp = datetime.now().strftime("%H:%M")
        return f"[{timestamp}] {msg}"

    def clear_logs(self) -> None:
        self.log.delete("1.0", END)
        self._append_log("[INFO] 日志已清空。")

    def scroll_to_bottom(self) -> None:
        self.log.see(END)

    def toggle_auto_scroll(self) -> None:
        next_status = "off" if self.auto_scroll.get() == "on" else "on"
        self.auto_scroll.set(next_status)
        status = "开启" if next_status == "on" else "关闭"
        self._append_log(f"[INFO] 自动滚动已{status}。")

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
        cmd = self._build_cmd(mode)
        self._append_log(f"[CMD] {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
            bufsize=0,
        )
        self.proc = proc
        self.status_text.set(f"运行中（{mode}）")
        threading.Thread(target=self._drain_output, args=(proc, mode), daemon=True).start()

    def stop_mode(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self._append_log("[INFO] 当前无运行中的任务。")
            return
        self.proc.terminate()
        self._append_log("[INFO] 已发送终止信号。")

    def _drain_output(self, proc: subprocess.Popen, mode: str) -> None:
        if proc.stdout is not None:
            for raw_line in iter(proc.stdout.readline, b""):
                line = self._decode_output_line(raw_line)
                self.root.after(0, self._append_log, line.rstrip("\n"))
        code = proc.wait()
        if self.proc is proc:
            self.root.after(0, self.status_text.set, f"已结束（{mode}, exit={code}）")

    @staticmethod
    def _decode_output_line(raw_line: bytes) -> str:
        for enc in ("utf-8", "gbk"):
            try:
                return raw_line.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw_line.decode("utf-8", errors="replace")

    def _refresh_loop(self) -> None:
        stats = collect_task_stats(self.output_root)
        self.stats_text.set(
            f"row={stats['rows']} | 总任务={stats['total_tasks']} | 完成={stats['completed']} | 失败={stats['failed']} | 待处理={stats['pending']}"
        )
        running = 1 if self.proc is not None and self.proc.poll() is None else 0
        self.thread_text.set(f"max_concurrency={self.max_concurrency} | workflow子进程={running} | monitor线程={threading.active_count()}")
        self.root.after(1000, self._refresh_loop)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    app = MonitorApp(DEFAULT_CONFIG)
    app.run()


if __name__ == "__main__":
    main()
