# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

# 各模式所需的检查项及级别
MODE_CHECKS: dict[str, dict[str, str]] = {
    "full": {
        "dep:pandas": "critical", "dep:rosbags": "critical",
        "dep:httpx": "critical", "dep:truststore": "critical", "dep:requests": "critical",
        "ffmpeg": "critical", "excel": "critical", "output_dir": "critical", "ai_config": "critical",
    },
    "download-only": {
        "dep:pandas": "critical", "dep:requests": "critical",
        "dep:rosbags": "skip", "dep:httpx": "skip", "dep:truststore": "skip",
        "ffmpeg": "skip", "excel": "critical", "output_dir": "critical", "ai_config": "skip",
    },
    "decode-only": {
        "dep:pandas": "skip", "dep:requests": "skip",
        "dep:rosbags": "critical", "dep:httpx": "skip", "dep:truststore": "skip",
        "ffmpeg": "critical", "excel": "skip", "output_dir": "critical", "ai_config": "skip",
    },
    "ai-only": {
        "dep:pandas": "skip", "dep:requests": "skip",
        "dep:rosbags": "skip", "dep:httpx": "critical", "dep:truststore": "critical",
        "ffmpeg": "skip", "excel": "skip", "output_dir": "critical", "ai_config": "critical",
    },
    "writeback-only": {
        "dep:pandas": "critical", "dep:requests": "skip",
        "dep:rosbags": "skip", "dep:httpx": "skip", "dep:truststore": "skip",
        "ffmpeg": "skip", "excel": "critical", "output_dir": "critical", "ai_config": "skip",
    },
}


def run_preflight(config_path: str | Path, mode: str = "full") -> dict[str, Any]:
    config_path = Path(config_path)
    checks: list[dict[str, Any]] = []
    level_map = MODE_CHECKS.get(mode, MODE_CHECKS["full"])

    cfg = _check_config(config_path, checks)
    _check_dependencies(checks, level_map)
    _check_ffmpeg(cfg, checks, level_map)
    _check_excel(cfg, checks, level_map)
    _check_output_dir(cfg, checks, level_map)
    _check_ai_config(cfg, checks, level_map)

    critical_failed = [c for c in checks if c["level"] == "critical" and c["status"] != "ok"]
    return {
        "passed": len(critical_failed) == 0,
        "mode": mode,
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
        f"[PREFLIGHT] {tag} mode={result.get('mode', 'full')} "
        f"(ok={result['ok']}, warn={result['warn']}, fail={result['fail']})"
    )
    for c in result["checks"]:
        icon = {"ok": "+", "warn": "~", "fail": "!", "skip": "-"}[c["status"]]
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


def _check_dependencies(checks, level_map):
    deps = [
        ("pandas", "Excel 解析"),
        ("rosbags", "ROS bag 读取"),
        ("httpx", "AI API 调用"),
        ("truststore", "系统证书链"),
        ("requests", "bag 下载"),
    ]
    for mod, desc in deps:
        key = f"dep:{mod}"
        level = level_map.get(key, "critical")
        if level == "skip":
            _add(checks, key, "skip", "skip", f"{desc} ({mod}) — 当前模式不需要")
            continue
        try:
            __import__(mod)
            _add(checks, key, level, "ok", f"{desc} ({mod})")
        except ImportError:
            _add(checks, key, level, "fail", f"缺少依赖: pip install {mod} ({desc})")


def _check_ffmpeg(cfg, checks, level_map):
    level = level_map.get("ffmpeg", "critical")
    if level == "skip":
        _add(checks, "ffmpeg", "skip", "skip", "当前模式不需要 ffmpeg")
        return
    ffmpeg_path = cfg.get("decoder", {}).get("ffmpeg_path", "ffmpeg")
    found = shutil.which(ffmpeg_path)
    if found:
        try:
            proc = subprocess.run(
                [ffmpeg_path, "-version"], capture_output=True, text=True, timeout=5
            )
            version_line = proc.stdout.split("\n")[0] if proc.stdout else "unknown"
            _add(checks, "ffmpeg", level, "ok", f"{found} ({version_line})")
        except Exception as e:
            _add(checks, "ffmpeg", level, "fail", f"ffmpeg 无法执行: {e}")
    else:
        _add(checks, "ffmpeg", level, "fail",
             f"ffmpeg 不在 PATH 中 (配置值: {ffmpeg_path})")


def _check_excel(cfg, checks, level_map):
    level = level_map.get("excel", "critical")
    if level == "skip":
        _add(checks, "excel", "skip", "skip", "当前模式不需要 Excel")
        return
    excel_path = cfg.get("excel", {}).get("path")
    if not excel_path:
        _add(checks, "excel", level, "fail", "config.excel.path 未配置")
        return
    p = Path(excel_path)
    if not p.exists():
        _add(checks, "excel", level, "fail", f"Excel 文件不存在: {p}")
        return
    try:
        import pandas as pd
        df = pd.read_excel(
            p,
            sheet_name=cfg.get("excel", {}).get("sheet", 0),
            header=cfg.get("excel", {}).get("header", 0),
            nrows=0,
        )
        _add(checks, "excel", level, "ok", f"{p} (列数={len(df.columns)})")
    except Exception as e:
        _add(checks, "excel", level, "fail", f"Excel 读取失败: {e}")


def _check_output_dir(cfg, checks, level_map):
    level = level_map.get("output_dir", "critical")
    if level == "skip":
        _add(checks, "output_dir", "skip", "skip", "当前模式不需要输出目录")
        return
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
        _add(checks, "output_dir", level, "ok", f"输出目录可写: {p}")
    except Exception as e:
        _add(checks, "output_dir", level, "fail", f"输出目录不可写: {p} ({e})")


def _check_ai_config(cfg, checks, level_map):
    level = level_map.get("ai_config", "critical")
    if level == "skip":
        _add(checks, "ai_config", "skip", "skip", "当前模式不需要 AI 配置")
        return
    ai_cfg = cfg.get("ai", {})
    if not ai_cfg.get("enabled", False):
        _add(checks, "ai_config", "info", "ok", "AI 未启用，跳过检查")
        return
    api_cfg = ai_cfg.get("api", {})
    missing = [k for k in ("base_url", "api_key", "user_id", "response_mode") if not api_cfg.get(k)]
    if missing:
        _add(checks, "ai_config", level, "fail", f"config.ai.api 缺少字段: {missing}")
    else:
        _add(checks, "ai_config", level, "ok",
             f"AI 配置完整 (base_url={api_cfg['base_url']})")
