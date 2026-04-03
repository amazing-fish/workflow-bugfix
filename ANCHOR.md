# Anchor 文档

## 版本
- 当前版本：`v0.3.1`
- 版本规则：`v主.次.修`
  - `feature`：新增能力，升级 `次`
  - `refactor`：重构与结构优化（不改外部能力），升级 `修`
  - `bugfix`：问题修复，升级 `修`

## 技术路径（稳定）
1. `workflow.py`
   - 负责任务编排（下载、解码、AI 推理并发）。
   - 产出：`row_summary.json`、`workflow_summary.json`。
2. `ai_runner.py`
   - 负责 sample 级 AI 调用、协议校验、输出归一化。
   - 产出：`sample_result_summary.json`、`ai_result_summary.json`。
3. 明细下沉策略
   - sample 级详细上下文写入 `sample*/ai/*.json`。
   - row/task/workflow 聚合摘要仅保留决策、统计与明细路径引用。
4. `monitor_gui.py`
   - 负责 workflow 可视化控制与运行日志展示。
   - 提供运行入口、实时状态刷新、日志交互能力（滚动/清空/置底）与分钟级时间戳。

## 修改日志（稳定）
- `v0.3.1` `bugfix`（2026-04-03）
  - 日志可观测性增强：
    - 所有 GUI 日志统一追加分钟级时间戳（`YYYY-MM-DD HH:MM`）。
    - 兼容已有命令日志、状态日志与子进程输出日志，便于排障定位。

- `v0.3.0` `feature`（2026-04-03）
  - 全面优化 GUI 日志交互体验：
    - 新增日志垂直滚动条，支持长日志快速回溯。
    - 新增“清空日志”按钮，支持一键重置日志窗口。
    - 新增“日志置底”按钮与“自动滚动开/关”，提升排障可控性。
  - 更新 Anchor 技术路径：补充 `monitor_gui.py` 在系统中的职责定义。

- `v0.2.16` `refactor`（2026-04-03）
  - 精简 `ANCHOR.md` 历史日志展示：
    - 保留近期关键版本的详细变更。
    - 老版本改为区间摘要，降低维护噪音并保持检索效率。

- `v0.2.15` `bugfix`（2026-04-03）
  - 修复 `ai_runner.py` 聚合统计与紧凑标签不一致：
    - `to_analysis_label` 兼容 `是/否/疑似` 与 `yes/no/suspected`。
    - `_aggregate_sequence_results` 基于 `compact_label` 统一计数。

- `v0.2.14` `bugfix`（2026-04-03）
  - 增加 row 级分析归档：
    - `row_summary.json`/`row_meta.json` 新增 `analysis`。
    - `workflow_summary.json` 的 `rows[]` 增加 `analysis` 引用。

- `v0.2.13` `bugfix`（2026-04-02）
  - 增加 task 级分析摘要与保留样本映射：
    - `analysis.count_line`：`rowX/tYY: [no:N,suspected:M,yes:K]`
    - `analysis.retained_line`：保留样本映射。
  - `task_meta.json` 中 `ai` 字段改为轻量摘要，完整明细下沉到 `ai_result_summary.json`。

- 历史版本（精简）
  - `v0.2.0 ~ v0.2.12`：
    - 新增监控 GUI、`--ai-only`/`--decode-only` 路径完善、下载重试与 bag/zip 校验增强、并发与稳定性修复。
  - `v0.1.1`：
    - 首次完成 summary 分层与明细下沉的结构化重构。
