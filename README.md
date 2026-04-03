# workflow

行级下载 + 解码 + AI 推理工作流，附带本地可视化监控面板（Tkinter）。

## 运行依赖

### Python 依赖

```bash
pip install pandas openpyxl requests httpx truststore rosbags
```

| 依赖 | 用途 |
|------|------|
| `pandas` + `openpyxl` | Excel 解析 |
| `requests` | bag 文件下载 |
| `httpx` + `truststore` | AI API 调用（系统证书链） |
| `rosbags` | ROS bag 文件读取 |

### 外部依赖

- **ffmpeg**：H.265 视频帧解码，需在 PATH 中可用，或通过 `config.decoder.ffmpeg_path` 指定路径
- **Excel 输入文件**：包含 issue 链接和 checker 文本的数据源，路径由 `config.excel.path` 指定

### 预检

启动时自动执行环境预检（`preflight.py`），覆盖配置、依赖、ffmpeg、Excel、输出目录、AI 配置。critical 级检查失败时 fail-fast，不进入正式流程。

## 执行模式

### CLI 模式

```bash
# 全流程：下载 → 解码 → AI 推理
python workflow.py --config config.json

# 仅下载 bag
python workflow.py --config config.json --download-only

# 仅解码（基于已下载的 bag）
python workflow.py --config config.json --decode-only

# 仅解码 + 强制删除 bag
python workflow.py --config config.json --decode-only --force-delete-bags

# 仅 AI 推理（基于已有 manifest + 图片）
python workflow.py --config config.json --ai-only

# 仅对单个 row 解码
python workflow.py --config config.json --decode-only --row-dir outputs/row2
```

### GUI 监控面板

```bash
python monitor_gui.py
```

面板提供：
- 四种启动按钮：全流程、仅下载、解码（删 bag）、直接 AI
- 实时任务统计：row 数、总任务、完成、失败、待处理
- 线程情况：并发上限、子进程数、线程数
- 日志面板：自动滚动、清空、置底

## 默认配置说明

`config.json` 作为默认配置持续演进。以下说明哪些字段可直接沿用，哪些需按环境调整。

### 需要按环境调整的字段

| 字段 | 说明 |
|------|------|
| `browser_headers.Authorization` | Bearer Token，需替换为当前有效的登录凭据 |
| `browser_headers.Cookie` | 浏览器 Cookie，需替换为当前会话 |
| `excel.path` | Excel 输入文件路径 |
| `excel.rows` | 要处理的 Excel 行号列表，空数组表示处理 `start_row` 到末尾的全部行 |
| `ai.api.api_key` | AI Workflow API Key |

### 可直接沿用的字段

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `base_url` | `https://console.di.adscloud...` | 数据平台入口 |
| `topics` | `camera_encoded_9~12` | 4 路鱼眼相机 topic |
| `sampling.before_frames` | `4` | 目标时间戳前采样帧数 |
| `sampling.after_frames` | `5` | 目标时间戳后采样帧数 |
| `decoder.ffmpeg_path` | `ffmpeg` | ffmpeg 路径 |
| `decoder.image_ext` | `png` | 输出图片格式 |
| `pipeline.max_concurrency` | `3` | 全局并发上限 |
| `cleanup.delete_bags_after_row` | `true` | row 完成后删除 bag |
| `ai.api.response_mode` | `streaming` | AI 调用模式（当前仅支持 streaming） |

## 输出目录结构

```
outputs/
├── download_summary.json          # 下载汇总
├── workflow_summary.json          # 全流程汇总
└── row2/                          # 每个 Excel 行一个目录
    ├── row_meta.json              # 行级元数据（链接、定位、下载路径、任务列表）
    ├── row_summary.json           # 行级聚合摘要（状态、统计、分析）
    ├── camera_encoded_*.bag       # bag 文件（完成后按配置删除）
    └── t01/                       # 每个碰撞时间戳一个 task 目录
        ├── manifest.json          # 解码清单（每路相机每帧的状态和路径）
        ├── task_meta.json         # 任务级摘要
        ├── ai_result_summary.json # AI 推理聚合结果
        └── sample05/              # 每个采样帧一个 sample 目录
            ├── frames/            # 解码后的图片
            │   ├── camera_encoded_9.png
            │   ├── camera_encoded_10.png
            │   ├── camera_encoded_11.png
            │   └── camera_encoded_12.png
            └── ai/                # AI 推理明细
                ├── selected_images.json
                ├── uploaded_files.json
                ├── workflow_payload.json
                ├── workflow_stream_result.json
                ├── workflow_raw_events.ndjson
                ├── workflow_final_schema_report.json
                ├── workflow_final_normalized.json
                └── sample_result_summary.json
```

### 主要输出文件说明

| 文件 | 层级 | 说明 |
|------|------|------|
| `download_summary.json` | workflow | 所有 row 的下载结果汇总 |
| `workflow_summary.json` | workflow | 全流程最终汇总（row 数、成功/失败统计） |
| `row_meta.json` | row | 行级元数据：issue 链接、定位信息、下载路径、任务列表及状态 |
| `row_summary.json` | row | 行级聚合：碰撞判定统计（yes/no/suspected）、保留样本列表 |
| `task_meta.json` | task | 任务级摘要：解码状态、AI 状态、错误信息 |
| `manifest.json` | task | 解码清单：每路相机每帧的时间戳、解码方法、图片路径 |
| `ai_result_summary.json` | task | AI 推理聚合：sample 序列结果、碰撞统计、保留样本 |
| `sample_result_summary.json` | sample | 单帧 AI 结果：碰撞判定、距离、摘要、重试信息 |

## 常见失败场景与排查

### 下载阶段

| 现象 | 可能原因 | 排查建议 |
|------|----------|----------|
| 所有 bag 下载异常 | `_looks_like_zip` 误判 | 确认 bag.py 版本包含 PK header 修复 |
| 401/403 错误 | Token/Cookie 过期 | 更新 `browser_headers` 中的 Authorization 和 Cookie |
| 连接超时 | 网络不通或 VPN 未连接 | 检查内网连通性，调整 `timeout_sec` |
| zip 解压失败 | 服务端返回异常响应 | 查看 `download_summary.json` 中的错误详情 |

### 解码阶段

| 现象 | 可能原因 | 排查建议 |
|------|----------|----------|
| ffmpeg 不可用 | 未安装或不在 PATH | `ffmpeg -version` 验证，或配置 `decoder.ffmpeg_path` |
| 解码失败率高 | H.265 流损坏 | 查看 `manifest.json` 中各帧的 `decode_method` 和 `status` |
| 帧数不足警告 | bag 中消息数少于窗口大小 | 正常现象，查看 `delta_to_center_sec` 确认实际偏移 |

### AI 阶段

| 现象 | 可能原因 | 排查建议 |
|------|----------|----------|
| API Key 无效 | Key 过期或错误 | 更新 `ai.api.api_key` |
| 输出为 None 触发重试 | 模型未返回有效结构化输出 | 查看 `sample*/ai/workflow_stream_result.json` |
| 502/504 网关错误 | AI 服务端压力 | 自动重试（60s 间隔），查看 `retry_count` 和 `retry_reasons` |
| enum 非法值重试 | 模型输出不在 是/否/疑似 范围 | 查看 `workflow_final_schema_report.json` 中的 `enum_errors` |

### 通用排查

- 启动前预检：`python -c "from preflight import run_preflight, format_preflight; print(format_preflight(run_preflight('config.json')))"`
- 查看单个 task 完整 AI 明细：`sample*/ai/` 目录下的 JSON 文件
- 查看 row 级碰撞统计：`row_summary.json` 中的 `analysis.count_line`
