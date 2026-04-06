# Anchor 文档

## 版本
- 当前版本：`v0.3.7`
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
- `v0.3.7` `bugfix`（2026-04-06）
  - 基于 F12 负载修复 `getObsId` 兼容性问题：
    - `files[]` 由仅传 `path` 升级为携带 `name/type/path/size` 的结构，兼容线上接口习惯。
    - `getObsId` 增加多 path 变体尝试（带/不带前导 `/`、带/不带 bucket 前缀），降低路径格式差异导致的空结果。
    - 下载最小尝试次数提升到 2 次，提升短暂空包/网关抖动场景恢复率。

- `v0.3.6` `bugfix`（2026-04-06）
  - 修复下载返回空 zip（`PK\x05\x06`）被误判为 raw bag 的问题：
    - zip 识别扩展为 `PK\x03\x04` / `PK\x05\x06` / `PK\x07\x08` 三类签名。
    - 空 zip 场景统一走 zip 分支并输出 `zip_entries/size`，避免落到 `File magic is invalid` 的误导报错。
    - 将典型空 zip 特征错误纳入可重试提示，提高短暂后端异常下的恢复概率。

- `v0.3.5` `bugfix`（2026-04-06）
  - 继续修复 `seqno/subSeqNo` 路径解析失败：
    - 命中 subtask 后若标准路径字段缺失，支持递归提取嵌套 `*file*path*` 字段值。
    - `event/list` 回退从单策略升级为多策略（`defectId`、`id=data_name/sub_seqno/seqno`、命中项的 `tideName/dataName`）。
    - 降低“命中 subtask 但路径缺失且回退失败”的出现概率。

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
