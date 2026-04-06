# Anchor 文档

## 版本
- 当前版本：`v0.3.8`
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
- `v0.3.8` `refactor`（2026-04-06）
  - 分支同步与合入准备：
    - 已完成 `work` 对 `main` 的代码同步，确保下载链路与文档版本一致；
    - 统一以 Anchor 作为技术路径与修改日志基线，降低后续合入偏差；
    - 保持版本号遵循 `v主.次.修`，并按 `feature/refactor/bugfix` 进行语义化维护。

- `v0.3.7` `bugfix`（2026-04-06）
  - 修复 `querySubTaskByType` 首屏返回空导致的误失败：
    - 根因：部分 case 在携带 `seqno` 过滤时返回空列表，导致提前报错；
    - 修复：改为双阶段检索：先 `subSeqno + seqno`，若第一页空则自动降级为仅 `subSeqno` 再检索；
    - 保留分页匹配逻辑，命中 `subSeqNo` 即返回 subtask，避免第 15 行这类空结果误判。

- `v0.3.6` `bugfix`（2026-04-06）
  - 修复 `taskId` 分区日期来源错误：
    - 废弃基于时间戳/时间字符串推测目录日期的策略；
    - 改为从 `querySubTaskByType` 返回的 `logfilePath` 直接解析准确分区路径与 bucket；
    - 基于 `logfilePath` 中的 `carjam_etoe/YYYY/MM/DD/<taskId>` 精确拼接 `archive`，用于后续 `queryMenu` 检索与 `getObsId`。

- `v0.3.5` `bugfix`（2026-04-06）
  - 修复 DI 链路下载定位不一致问题（优先走 F12 实测链路）：
    - 新增 `onlineVisualQuery` 预查询：先用链接 `subSeqno` 获取标准化 `result.subSeqno`。
    - `querySubTaskByType` 改为以 `subSeqno` 为主检索，并保留分页兜底（最多 20 页）。
    - 下载定位新增 `taskId -> queryMenu -> getObsId(文件模式)` 路径：
      - 基于 `taskId` 生成 `carjam_etoe/YYYY/MM/DD/<taskId>/archive` 候选目录；
      - 自动组合候选日期（时间戳/字符串/tideName，含 ±1 天）以适配日期分区差异；
      - 通过 `queryMenu` 命中具体 `*.bag` 文件后，按文件详情请求 `getObsId`。
    - 保留旧 `transfer_path` 与 `event/list` 回退，确保历史链路兼容。

- `v0.3.4` `bugfix`（2026-04-06）
  - 修复部分 bag 下载前置定位失败：
    - `querySubTaskByType` 增加分页检索（最多 20 页），避免仅查首页导致 `subSeqNo` 命中失败。
    - `subSeqNo` 匹配增强：兼容 `subSeqNo/subSeqno` 字段并做大小写/空白归一。
    - transfer path 提取增强：除 `carjamFilePath/replayFilePath` 外，兼容 `transferFilePath/filePath`。
    - 命中 subtask 但路径缺失时，增加 `event/list` 回退解析，降低因接口字段不齐导致的失败率。

- `v0.3.3` `bugfix`（2026-04-03）
  - AI 重试策略增强：
    - 除 `None` 外，若结构化输出出现 enum 非法值，也会触发重试。
    - 明确 AI 调用失败重试场景：`Server disconnected without sending a response`、`502 Bad Gateway`、`504 Gateway Time-out`。
    - 命中上述调用失败后，固定等待 60 秒再重试。
    - sample 摘要新增 `retry_count` 与 `retry_reasons`，并保留 `none_retry_count` 统计。

- `v0.3.2` `bugfix`（2026-04-03）
  - 日志时间格式优化：
    - GUI 日志时间戳改为仅保留分钟（`HH:MM`），去除年月日。
    - 保持日志排序与定位能力，同时降低视觉噪音。

- `v0.3.1` `bugfix`（2026-04-03）
  - 日志可观测性增强：
    - 所有 GUI 日志统一追加分钟级时间戳（初始格式：`YYYY-MM-DD HH:MM`）。
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
