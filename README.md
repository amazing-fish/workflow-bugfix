# workflow

行级下载 + 解码 + AI 推理工作流，新增了一个本地可视化监控面板（Tkinter）。

## 快速开始

```bash
python workflow.py --config config.json
```

## 可视化监控（按钮启动）

```bash
python monitor_gui.py
```

监控面板包含：
- 读取到的任务总量（总任务/完成/失败/待处理）。
- 线程情况（配置并发上限、当前 workflow 子进程数、监控进程线程数）。
- 四种启动按钮：
  - 全流程
  - 仅下载 bag
  - 解码图片（删除 bag）
  - 直接调用 AI（基于已有 manifest + 图片）

## CLI 模式

- 全流程：
  ```bash
  python workflow.py --config config.json
  ```
- 仅下载：
  ```bash
  python workflow.py --config config.json --download-only
  ```
- 仅解码（对已有输出目录）：
  ```bash
  python workflow.py --config config.json --decode-only
  ```
- 仅 AI（对已有图片+manifest）：
  ```bash
  python workflow.py --config config.json --ai-only
  ```
- 解码后强制删 bag（覆盖 config）：
  ```bash
  python workflow.py --config config.json --decode-only --force-delete-bags
  ```
