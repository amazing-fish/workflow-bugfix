# Anchor 文档

## 版本
- 当前版本：`v0.1.1`
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
- `v0.1.1` `refactor`（2026-04-01）
  - 重构 summary 分层职责：
    - `workflow_summary.json` 不再内嵌全量 row 详情，仅保留 row 级统计 + 路径。
    - `row_summary.json` 不再携带全量 task 结果，仅保留 task 级简表 + 路径。
    - `sample_result_summary.json`（AR 结果摘要）移除冗余字段（图片路径、各类 ID、模型/工作流细节），保留判定结论与必要统计，并通过 `detail_paths` 关联明细文件。
