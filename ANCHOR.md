# Anchor 文档

## 版本
- 当前版本：`v0.2.0`
- 版本规则：`v主.次.修`
  - `feature`：新增能力，升级 `次`
  - `refactor`：重构与结构优化（不改外部能力），升级 `修`
  - `bugfix`：问题修复，升级 `修`

## 技术路径（稳定）
1. `workflow.py`
   - 负责任务编排（下载、解码、AI 推理并发）。
   - 负责产出两层聚合摘要：
     - `row_summary.json`（row 级）
     - `workflow_summary.json`（workflow 级）
2. `ai_runner.py`
   - 负责 sample 级调用 AI、协议校验、输出归一化。
   - 产出 `sample_result_summary.json` 与 `ai_result_summary.json`。
3. 明细下沉策略
   - 详细上下文（图片路径、上传文件、workflow payload、流式事件、schema 校验、归一化原文）写入 `sample*/ai/*.json`。
   - 聚合摘要仅保留决策与状态字段 + 明细路径。

## 修改日志（稳定）
- `v0.2.0` `feature`（2026-04-02）
  - 新增本地可视化监控面板 `monitor_gui.py`：
    - 支持展示任务总量、完成/失败/待处理统计。
    - 支持展示线程情况（`max_concurrency`、workflow 子进程、monitor 线程数）。
    - 新增四类启动按钮：全流程、仅下载 bag、解码图片（删除 bag）、直接调用 AI。
  - 扩展 `workflow.py` 启动模式：
    - 新增 `--ai-only`，支持对已解码产物直接执行 AI。
    - 新增 `--force-delete-bags`，可覆盖配置强制清理 bag。
- `v0.1.1` `refactor`（2026-04-01）
  - 重构 summary 分层职责：
    - `workflow_summary.json` 不再内嵌全量 row 详情，仅保留 row 级统计 + 路径。
    - `row_summary.json` 不再携带全量 task 结果，仅保留 task 级简表 + 路径。
    - `sample_result_summary.json`（AR 结果摘要）移除冗余字段（图片路径、各类 ID、模型/工作流细节），保留判定结论与必要统计，并通过 `detail_paths` 关联明细文件。
