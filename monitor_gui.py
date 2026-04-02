# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, Button, Frame, Label, StringVar, Text, Tk


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
        Button(btns, text="停止当前任务", command=self.stop_mode).pack(side=RIGHT, padx=6)

        self.log = Text(self.root, wrap="word")
        self.log.pack(fill=BOTH, expand=True, padx=12, pady=8)

    def _append_log(self, msg: str) -> None:
        self.log.insert(END, msg + "\n")
        self.log.see(END)

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
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.status_text.set(f"运行中（{mode}）")
        threading.Thread(target=self._drain_output, daemon=True).start()

    def stop_mode(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self._append_log("[INFO] 当前无运行中的任务。")
            return
        self.proc.terminate()
        self._append_log("[INFO] 已发送终止信号。")

    def _drain_output(self) -> None:
        assert self.proc is not None
        for line in self.proc.stdout or []:
            self.root.after(0, self._append_log, line.rstrip("\n"))
        code = self.proc.wait()
        self.root.after(0, self.status_text.set, f"已结束（exit={code}）")

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
