# Anchor 文档

## 版本
- 当前版本：`v0.2.12`
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
- `v0.2.12` `bugfix`（2026-04-02）
  - 调整 `bag.py` 下载节奏控制策略：
    - 单个 bag 下载并校验成功后固定额外等待 `1s`，降低连续请求抖动风险。
    - 第一次失败重试前固定等待 `2s`，第二次失败重试前固定等待 `4s`（指数退避）。
    - 该等待策略优先于旧的可配置退避秒数约定，确保行为与运维预期一致。
- `v0.2.11` `bugfix`（2026-04-02）
  - 修复 `bag.py` 下载重试判定过度依赖错误文案的问题：
    - `_is_retriable_download_error` 改为异常类型优先判定，直接识别 `zipfile.BadZipFile`、`requests.Timeout`、`requests.ConnectionError`。
    - 支持沿 `__cause__ / __context__` 遍历异常链，避免包装异常导致漏判。
    - 保留关键字匹配作为兜底，仅用于少数业务错误文案（如“zip 内无 .bag”）。
- `v0.2.10` `bugfix`（2026-04-02）
  - 修复 `bag.py` 下载阶段“zip 内无 bag”易瞬时失败的问题：
    - 在 `_download_by_obs_id` 增加重试机制（默认重试 2 次，共最多 3 次尝试）。
    - 对 `download 返回 zip，但未找到 .bag`、`BadZipFile`、超时/连接中断等异常执行指数退避重试（`backoff = 基础秒数 * attempt`）。
    - 重试耗尽后抛出聚合异常，明确标注“已重试 N 次”与最后一次错误原因，便于排障。
    - 新增可配置项：`download_retry_times`、`download_retry_backoff_sec`（缺省分别为 2 和 1.0）。
- `v0.2.9` `bugfix`（2026-04-02）
  - 修复 `bag.py` 的 zip 识别边界问题：
    - 兼容识别 `PK\x05\x06`（空 zip）与 `PK\x07\x08` 等 zip 文件头，避免被误判为原始响应直存分支。
    - 对非典型头但结构合法的 zip，回退使用 `zipfile.is_zipfile` 识别，统一进入 zip 解包逻辑。
    - 当 zip 中无 `.bag` 成员时，将明确抛出“zip 内无 bag”异常，避免误报 `File magic is invalid` 干扰定位。
- `v0.2.8` `bugfix`（2026-04-02）
  - 修复 `bag.py` 的 bag 下载校验缺失问题：
    - 对“原始响应直存 bag”与“zip 解包提取 bag”两种下载路径统一新增文件头校验（`#ROSBAG`）。
    - 当下载结果不是合法 bag 时立即抛出异常，并输出 `source/size/prefix_hex/prefix_text` 诊断信息，避免延迟到解码阶段才报 `File magic is invalid`。
- `v0.2.7` `bugfix`（2026-04-02）
  - 修复 `workflow.py` 的 `--ai-only` 执行模型：
    - 由串行逐 row/逐 task 执行改为任务级线程池并发执行。
    - 并发上限与全流程一致，统一使用 `pipeline.max_concurrency` 控制。
    - 每个 task 完成后立即落盘 `task_meta.json`，并在 row 级 `pending=0` 时收敛 `row_summary.json`。
  - 稳定性优化：
    - `workflow_summary.json` 的 row 列表按 `row_dir` 排序，避免并发导致输出顺序抖动。
- `v0.2.6` `bugfix`（2026-04-02）
  - 修复 `workflow.py` 的仅解码全量模式初始化行为：
    - `run_decode_for_all_rows()` 不再调用 `ai_processor.prepare()`，避免 `--decode-only` 仍依赖 AI `/parameters` 接口可用性。
    - 保证仅解码流程在 AI 服务不可用时仍可独立执行。
  - 修复 `ai_runner.py` 的 AI-only 可重跑问题：
    - `sample` 序列收集时新增图片存在性校验，仅纳入磁盘上仍存在图片的样本。
    - 当此前已清理 `collision_pred=否` 的样本目录后，后续 `--ai-only` 重跑不会再因缺图产生确定性失败。
- `v0.2.5` `bugfix`（2026-04-02）
  - 修复 `workflow.py` 的仅解码路径行为：
    - `--decode-only` 与监控面板“解码图片（删除bag）”模式下，仅执行解码，不再触发 AI 推理。
    - 全流程与 `--ai-only` 仍保持原有 AI 行为。
  - 修复 `ai_runner.py` 的样本清理行为：
    - AI 完成后新增按判定清理：自动删除 `collision_pred=否` 的 `sample` 目录，保留其余样本。
    - 在 `ai_result_summary.json` 增加 `cleanup` 字段，记录删除结果与失败原因，便于追踪。
- `v0.2.4` `bugfix`（2026-04-02）
  - 修复 `ai_runner.py` 中 `none_retry_count` 统计偏大的问题：
    - 当最终一次尝试仍为 `None` 且已无后续重试机会时，不再累计重试次数。
    - `none_retry_count` 仅统计真实发生的重试次数（最大为 3），避免监控统计误判。
- `v0.2.3` `bugfix`（2026-04-02）
  - 修复 `ai_runner.py` 在 sample 级输出为 `None` 时缺少重试的问题：
    - 新增 `None` 输出判定逻辑（兼容 `None` 与字符串 `"None"`）。
    - 当 `collision_pred` 为 `None` 时自动重试该 case，最多重试 3 次。
    - 在 `sample_result_summary.json` 增加 `none_retry_count` 字段，记录实际重试次数，便于排障与统计。
- `v0.2.2` `bugfix`（2026-04-02）
  - 修复 `monitor_gui.py` Windows 控制台编码导致的日志线程崩溃问题：
    - 子进程输出改为二进制读取并做多编码解码（`utf-8` -> `gbk` -> `replace` 回退），避免 `UnicodeDecodeError` 中断监控线程。
- `v0.2.1` `bugfix`（2026-04-02）
  - 修复 `workflow.py` 仅 AI 重跑时的陈旧错误字段问题：
    - `run_ai_for_all_rows()` 在任务 AI 成功后会清理旧 `error` 字段，避免成功任务遗留历史失败信息。
  - 修复 `monitor_gui.py` 日志线程绑定问题：
    - 输出读取线程改为绑定固定 `Popen` 句柄，避免快速重启任务时线程误关联到新进程并更新错误状态。
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
