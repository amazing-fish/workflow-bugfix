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
- `v0.3.9` `bugfix`（2026-04-06）
  - `bag_probe.py` 增加“成功请求头重放”能力：
    - 新增 `--headers-file`（通用）及 `--menu-headers-file` / `--getobsid-headers-file` / `--download-headers-file`（分接口）。
    - 新增 `--exact-headers`，可严格按 F12 请求头发起探测，不与配置头混合。
    - 报告输入区增加 header keys 回显，便于核对脚本实际使用的请求头集合。

- `v0.3.8` `bugfix`（2026-04-06）
  - 基于维测结论修复 `bag.py` 的 `getObsId` 取值策略：
    - 增加候选 payload 顺序：`browser_like_slash_bucket` → `minimal_slash_bucket` → `minimal_legacy`。
    - 默认优先 `files[].path=/bucket/...`，并附带 `name/type/userName/dataType` 以兼容平台真实下载链路。
    - 若某候选无 `result`，自动回退到下一个候选，降低“22字节 zip / 0字节 body”命中概率。

- `v0.3.7` `bugfix`（2026-04-06）
  - 修复 `bag_probe.py` 推荐策略偏差：
    - `--file-size` 未传时不再生成 `browser_like_size` 分支，避免“同 payload 重复探测”噪音。
    - `analysis` 推荐从“首个成功”改为“按质量排序”（`rosbag` 优先于 `zip_with_bag`）。
    - `analysis` 增加 `recommended_get_obs_id_case` 与 `all_success_cases`，便于直接回灌主流程参数。

- `v0.3.6` `bugfix`（2026-04-06）
  - 增强 `bag_probe.py` 兼容维测能力：
    - 增加 `getObsId` 路径形态矩阵探测（`obs://`、`/bucket/...`、`/no_bucket/...`、`bucket/...`）。
    - 增加 `--file-size`，支持复刻浏览器 payload 的 `files[].size` 分支。
    - 下载探测新增质量分型（`rosbag` / `zip_with_bag` / `empty_zip_22` / `empty_body`）。
    - 报告新增 `analysis` 自动建议，直接给出推荐兼容 case。

- `v0.3.5` `bugfix`（2026-04-06）
  - 新增 bag 下载兼容维测脚本 `bag_probe.py`：
    - 自动探测 `downloadMenu`、多种 `getObsId` 负载（minimal / browser_like / browser_like_with_size0）。
    - 针对候选 `obs_id` 实测下载并输出内容判型（`#ROSBAG` / zip 内含 `.bag` / 22 字节空 zip）。
    - 生成 `bag_probe_report.json`，用于快速定位“仅返回 zip 头”问题与选择兼容参数。

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
