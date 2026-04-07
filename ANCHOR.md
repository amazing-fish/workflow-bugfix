# Anchor 文档

## 版本
- 当前版本：`v0.3.9`
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
- `v0.3.9` `refactor`（2026-04-07）
  - 精简 Anchor 修改日志结构：
    - 按主题合并同类变更（DI 检索链路、GUI 日志体验、AI 统计与摘要）；
    - 保留近期关键版本细节，历史版本收敛为区间摘要；
    - 保持技术路径稳定描述不变，降低文档维护噪音。

- `v0.3.8` `bugfix`（2026-04-07）
  - 时间戳缺失提示增强：
    - 当 checker 文本为空或未匹配到碰撞时间戳时，失败原因统一为 `未检测到时间戳`；
    - task 在 `target_ts` 缺失时，同步输出 `未检测到时间戳`，便于 GUI 与回填统一展示。
  - task 级时间戳记录增强：
    - 在 `ai_result_summary.json` 的 `sequence_results[]` 中新增 `sample_ts`；
    - 在 task 级 `analysis` 中新增 `sample_timestamps` 与 `sample_timestamp_line`，记录每个 sample 对应时间戳；
    - 保留样本行 `retained_line` 追加 `@timestamp`，row 级聚合同步透传该信息。

- `v0.3.4 ~ v0.3.7` `bugfix`（2026-04-06）
  - DI 检索与下载定位链路稳定性修复（同类合并）：
    - `querySubTaskByType` 增强：分页检索、`subSeqNo/subSeqno` 兼容、首屏空结果自动降级检索；
    - `taskId` 分区定位改为基于 `logfilePath` 精确解析，修复日期推断误差；
    - 下载定位补齐 `taskId -> queryMenu -> getObsId` 文件模式链路，并保留旧链路回退；
    - transfer path / event 回退增强，降低字段不齐与首页空结果导致的误失败。

- `v0.3.3` `bugfix`（2026-04-03）
  - AI 重试策略增强：
    - 除 `None` 外，若结构化输出出现 enum 非法值，也会触发重试。
    - 明确 AI 调用失败重试场景：`Server disconnected without sending a response`、`502 Bad Gateway`、`504 Gateway Time-out`。
    - 命中上述调用失败后，固定等待 60 秒再重试。
    - sample 摘要新增 `retry_count` 与 `retry_reasons`，并保留 `none_retry_count` 统计。

- `v0.3.0 ~ v0.3.2` `feature+bugfix`（2026-04-03）
  - GUI 日志体验与可观测性优化（同类合并）：
    - 新增滚动条、清空日志、日志置底、自动滚动开关；
    - 日志统一追加分钟级时间戳，并优化为 `HH:MM` 显示以降低噪音；
    - 同步补充 `monitor_gui.py` 技术路径职责说明。

- `v0.2.13 ~ v0.2.16` `bugfix+refactor`（2026-04-02 ~ 2026-04-03）
  - AI 统计与分层摘要稳定化（同类合并）：
    - task/row/workflow 逐级补齐 analysis 与保留样本映射；
    - `to_analysis_label` 与聚合计数逻辑对齐，修复标签统计不一致；
    - `task_meta.ai` 轻量化，完整明细下沉至 `ai_result_summary.json`；
    - Anchor 历史日志首轮精简，形成“关键版本细节 + 区间摘要”模式。

- 历史版本（区间摘要）
  - `v0.2.0 ~ v0.2.12`：监控 GUI、运行模式完善（`--ai-only`/`--decode-only`）、下载重试与 bag/zip 校验增强、并发稳定性修复。
  - `v0.1.1`：首次完成 summary 分层与明细下沉的结构化重构。
