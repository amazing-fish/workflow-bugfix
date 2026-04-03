# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def run_preflight(config_path: str | Path) -> dict[str, Any]:
    config_path = Path(config_path)
    checks: list[dict[str, Any]] = []
    cfg = _check_config(config_path, checks)
    _check_dependencies(checks)
    _check_ffmpeg(cfg, checks)
    _check_excel(cfg, checks)
    _check_output_dir(cfg, checks)
    _check_ai_config(cfg, checks)

    critical_failed = [c for c in checks if c["level"] == "critical" and c["status"] != "ok"]
    return {
        "passed": len(critical_failed) == 0,
        "total": len(checks),
        "ok": sum(1 for c in checks if c["status"] == "ok"),
        "warn": sum(1 for c in checks if c["status"] == "warn"),
        "fail": sum(1 for c in checks if c["status"] == "fail"),
        "checks": checks,
    }


def format_preflight(result: dict[str, Any]) -> str:
    lines = []
    tag = "PASSED" if result["passed"] else "FAILED"
    lines.append(
        f"[PREFLIGHT] {tag} (ok={result['ok']}, warn={result['warn']}, fail={result['fail']})"
    )
    for c in result["checks"]:
        icon = {"ok": "+", "warn": "~", "fail": "!"}[c["status"]]
        lines.append(f"  [{icon}] {c['name']}: {c['message']}")
    return "\n".join(lines)


def _add(checks, name, level, status, message):
    checks.append({"name": name, "level": level, "status": status, "message": message})


def _check_config(config_path, checks):
    if not config_path.exists():
        _add(checks, "config", "critical", "fail", f"配置文件不存在: {config_path}")
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        _add(checks, "config", "critical", "ok", f"配置文件加载成功: {config_path}")
        return cfg
    except Exception as e:
        _add(checks, "config", "critical", "fail", f"配置文件解析失败: {e}")
        return {}


def _check_dependencies(checks):
    deps = [
        ("pandas", "Excel 解析"),
        ("rosbags", "ROS bag 读取"),
        ("httpx", "AI API 调用"),
        ("truststore", "系统证书链"),
        ("requests", "bag 下载"),
    ]
    for mod, desc in deps:
        try:
            __import__(mod)
            _add(checks, f"dep:{mod}", "critical", "ok", f"{desc} ({mod})")
        except ImportError:
            _add(checks, f"dep:{mod}", "critical", "fail", f"缺少依赖: pip install {mod} ({desc})")


def _check_ffmpeg(cfg, checks):
    ffmpeg_path = cfg.get("decoder", {}).get("ffmpeg_path", "ffmpeg")
    found = shutil.which(ffmpeg_path)
    if found:
        try:
            proc = subprocess.run(
                [ffmpeg_path, "-version"], capture_output=True, text=True, timeout=5
            )
            version_line = proc.stdout.split("\n")[0] if proc.stdout else "unknown"
            _add(checks, "ffmpeg", "critical", "ok", f"{found} ({version_line})")
        except Exception as e:
            _add(checks, "ffmpeg", "critical", "fail", f"ffmpeg 无法执行: {e}")
    else:
        _add(checks, "ffmpeg", "critical", "fail",
             f"ffmpeg 不在 PATH 中 (配置值: {ffmpeg_path})")


def _check_excel(cfg, checks):
    excel_path = cfg.get("excel", {}).get("path")
    if not excel_path:
        _add(checks, "excel", "critical", "fail", "config.excel.path 未配置")
        return
    p = Path(excel_path)
    if not p.exists():
        _add(checks, "excel", "warn", "warn",
             f"Excel 文件不存在: {p} (仅 decode-only/ai-only 可跳过)")
        return
    try:
        import pandas as pd
        df = pd.read_excel(
            p,
            sheet_name=cfg.get("excel", {}).get("sheet", 0),
            header=cfg.get("excel", {}).get("header", 0),
            nrows=0,
        )
        _add(checks, "excel", "critical", "ok", f"{p} (列数={len(df.columns)})")
    except Exception as e:
        _add(checks, "excel", "critical", "fail", f"Excel 读取失败: {e}")


def _check_output_dir(cfg, checks):
    output_root = (
        cfg.get("output", {}).get("root_dir")
        or cfg.get("excel", {}).get("output_dir")
        or "outputs"
    )
    p = Path(output_root)
    try:
        p.mkdir(parents=True, exist_ok=True)
        test_file = p / ".preflight_test"
        test_file.write_text("ok")
        test_file.unlink()
        _add(checks, "output_dir", "critical", "ok", f"输出目录可写: {p}")
    except Exception as e:
        _add(checks, "output_dir", "critical", "fail", f"输出目录不可写: {p} ({e})")


def _check_ai_config(cfg, checks):
    ai_cfg = cfg.get("ai", {})
    if not ai_cfg.get("enabled", False):
        _add(checks, "ai_config", "info", "ok", "AI 未启用，跳过检查")
        return
    api_cfg = ai_cfg.get("api", {})
    missing = [k for k in ("base_url", "api_key", "user_id", "response_mode") if not api_cfg.get(k)]
    if missing:
        _add(checks, "ai_config", "critical", "fail", f"config.ai.api 缺少字段: {missing}")
    else:
        _add(checks, "ai_config", "critical", "ok",
             f"AI 配置完整 (base_url={api_cfg['base_url']})")
