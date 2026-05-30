# CLAUDE.md/AGENTS.md

This file provides guidance to Claude-Code/Codex when working with code in this repository.

# **关键语言要求**
你必须完全使用 **简体中文** 进行交互、思考和汇报。

## 项目概述

Agent Remote 是一个双端共享 Claude/Codex CLI 工具。通过飞书客户端和终端客户端并发连接同一个 AI 会话，实现协作式对话。

**本项目是纯客户端**——PTY server 运行时已迁移到 `agents-remote-core`（作为 editable 包依赖）。本项目负责：
- CLI 快捷入口（`cla`/`cl`/`cx`/`cdx`）
- 飞书机器人客户端（WebSocket + 卡片渲染）
- 终端客户端（raw mode 输入转发）

## 架构

```
agents-remote-core (包依赖)        agents-remote (本项目)
┌──────────────────────┐           ┌──────────────────────────┐
│  PTY Server          │           │  agent_remote.py (CLI)   │
│  ├─ HookHarness      │           │  client/client.py (终端)  │
│  ├─ OutputWatcher     │           │  lark_client/ (飞书)     │
│  ├─ Parsers           │           │    ├─ main.py            │
│  └─ SharedStateWriter │           │    ├─ lark_handler.py    │
└──────────┬───────────┘           │    ├─ shared_memory_poller│
           │                       │    ├─ card_builder.py     │
           ▼                       │    ├─ session_bridge.py   │
  /tmp/agents-remote/              │    └─ card_service.py     │
  ├─ <hash>.mq   ← 共享内存 ──────┤  utils/                   │
  ├─ <hash>.sock ← Unix Socket ───┤    ├─ shared_state_reader │
  └─ <hash>_hooks/  (FIFO)        │    ├─ session.py          │
                                   │    ├─ protocol.py         │
                                   │    └─ components.py       │
                                   └──────────────────────────┘
```

### 通信通道

| 通道 | 方向 | 用途 |
|------|------|------|
| **共享内存 `.mq`** | core server → poller（单向） | ClaudeWindow 全量快照（blocks + hook_state） |
| **Unix Socket `.sock`** | 双向 | INPUT / OUTPUT / RESIZE / PermissionResponse / QuestionResponse |
| **FIFO `events.fifo`** | CLI hook → core server | SessionStart / Stop / PreToolUse / PermissionRequest 等事件 |
| **响应文件 `resp_*`** | core server → hook 脚本 | 权限决策 / AskUserQuestion 答案 |

### 两种状态判断模式

lark 客户端自动适配：

| 模式 | 条件 | 轮次检测 | 就绪判断 |
|------|------|---------|---------|
| **hook 模式** | `hook_state` ≠ None | `turn_complete` True→False 翻转 | `turn_complete` 权威信号 |
| **解析模式** | `hook_state` = None | UserInput block_id 变化 | streaming + status_line 启发式 + 去抖 |

> 注：上表的「轮次检测」用于普通流式卡片。**简洁模式（`_do_card_update_simple`）的分轮统一走 UserInput 基准**（最后一条用户消息 = 一张卡），不随 hook 的 `turn_complete` 翻转——否则 `round_start` 会卡在很靠前（空闲 attach 时停在 0），把整段历史塞进一张卡，频繁触发飞书元素超限并丢失收尾帧。`turn_complete` 在简洁模式仅用于就绪判断。

### 简洁模式元素超限处理

简洁路径与普通路径一致地处理 `update_card` 返回的元素超限哨兵（`is_element_limit`）：冻结当前卡（完整内容）+ 起新卡承接，并同步 `simple_round_start` 到新卡起点。渲染真失败时**不写 `content_hash`**，保证下一轮重试，避免收尾帧永久丢失。

## 职责分界

| 层 | 职责 | 禁止事项 |
|----|------|---------|
| **agents-remote-core** | PTY 管理、终端解析、hook 注入、共享内存写入 | — |
| **本项目 lark_client/** | 读共享内存 → 飞书卡片渲染、用户交互处理 | 严禁对内容做字符串修复；内容不对应修 core |
| **本项目 agent_remote.py** | CLI 入口、tmux 会话管理、env snapshot | 不直接操作 PTY |

## 文件结构

```
agents-remote/
├── agent_remote.py            # CLI 入口（start/attach/list/kill/lark）
├── client/client.py           # 终端客户端（raw mode 输入转发 + OSC 标题栏状态指示）
│
├── lark_client/               # 飞书客户端
│   ├── main.py                # WebSocket 入口，事件分发，action 路由
│   ├── lark_handler.py        # 命令路由、会话管理、hook 多问题答案收集
│   ├── session_bridge.py      # Unix Socket 桥接（send_input/send_*_response）
│   ├── shared_memory_poller.py # 轮询 .mq → hash diff → 卡片创建/更新/冻结
│   ├── card_builder.py        # 四层卡片结构 + hook 按钮 + 多问题/多选
│   ├── card_service.py        # 飞书卡片 API
│   └── config.py              # 配置加载
│
├── utils/
│   ├── shared_state_reader.py # 共享内存读端（从 core 的 SharedStateWriter 读取）
│   ├── protocol.py            # 消息协议（JSON + \n，9 种消息类型）
│   ├── session.py             # socket 路径、会话生命周期、tmux 操作
│   └── components.py          # 数据模型（OutputBlock/UserInput/StatusLine 等）
│
├── tests/
│   ├── test_stream_poller.py  # 流式卡片模型单元测试
│   ├── test_format_unit.py    # 格式化逻辑单元测试
│   └── ...                    # 集成测试（需活跃会话）
│
├── pyproject.toml             # agents-remote-core 作为 editable 包依赖
└── init.sh                    # 安装快捷命令（cla/cl/cx/cdx）
```

## 依赖关系

```toml
# pyproject.toml
[project]
dependencies = ["agents-remote-core", ...]

[tool.uv.sources]
agents-remote-core = { path = "../agents-remote-core", editable = true }
```

- `agents-remote-core` 作为 editable 包依赖安装在本项目 venv 中
- 改 core 代码即生效，不用重装
- 启动会话时通过 `uv run --project <本项目> agents-remote-core start ...` 调用

## 常用命令

```bash
# 安装依赖（含 core editable 包）
uv sync

# 快捷命令（需运行 init.sh 配置）
cla                    # 启动飞书客户端 + Claude（当前目录为会话名）
cl                     # 同 cla，跳过权限确认
cx                     # 启动 Codex（跳过权限确认）
cdx                    # 启动 Codex（需确认权限）

# 会话管理
agents-remote start <会话名> [-- claude 参数]
agents-remote attach <会话名>
agents-remote list
agents-remote kill <会话名>

# 飞书客户端
agents-remote lark start|stop|restart|status
```

## 测试

```bash
# 单元测试（无需网络和服务）
uv run python3 tests/test_stream_poller.py    # 流式卡片模型
uv run python3 tests/test_format_unit.py      # 格式化逻辑

# 集成测试（需先启动会话）
uv run python3 tests/test_integration.py
uv run python3 tests/test_e2e.py
```

无 pytest 配置，测试文件均为独立脚本。

## 终端客户端状态指示（标题栏）

`client/client.py` 在 raw-mode 透传之外，额外起一个轮询任务读 `.mq` 共享内存的 `hook_state`，把会话状态写进终端窗口标题（OSC 0 序列）。用 OSC 标题而非屏幕底部状态条，是因为 `cla` 终端是纯透传、claude 占满全屏 TUI，OSC 只改标题、不占屏幕行，与 claude 渲染零冲突。

| hook_state | 标题栏 |
|------------|--------|
| `waiting_permission` / `pending_permission` | `⏳ 等待权限确认 · <名>` |
| `pending_question` | `❓ 等待回答 · <名>` |
| `turn_complete=False` + `active_tool` | `⚙ 运行中 <工具> · <名>` |
| `turn_complete=True` | `✓ <名>` |
| `hook_state` 为 None（解析模式） | `<名>`（无权威信号，不猜状态） |

目的：区分"真的在跑工具"和"卡住等飞书回复"，破解把 hook 拦截误判成工具卡死的盲区。

## 飞书卡片四层结构

| 层 | 内容 | 来源 |
|----|------|------|
| **内容区** | OutputBlock / UserInput / PlanBlock / SystemBlock | blocks 列表 |
| **状态区** | status_line + bottom_bar + agent_panel + option_block 文本 | 状态型组件 |
| **交互区** | hook 问题表单（select_static/checker，一次提交）/ 权限按钮 / OptionBlock 降级按钮 | hook_state 或 option_block |
| **菜单** | ⚡菜单 + 🔌断开 + Enter↵ + ⌨️快捷键面板（方向键 / 直发命令 Yes·继续·/compact） | 固定 |

### AskUserQuestion 多问题交互（hook 模式）

- **全部问题铺开在一个 `form` 表单里，一次性提交**（取代旧的"逐题 callback + 重渲染"状态机，消除其竞态）
- 单选 → `select_static` 下拉（option `value` = 选项 label）；多选 → 平铺 `checker` 复选框（回传 bool）
- 提交按钮 `action_type: form_submit`（无自定义 value）；`main.py` 按表单组件 name 的 `hq_` 前缀路由到 `handle_hook_questions_submit`，`request_id` 由 handler 从 `hook_state` 现取
- 组件命名：单选 `hq_<题序>`，多选 `hq_<题序>_<项序>`
- **pending_question 期间 poller 冻结卡片**（`StreamTracker.question_frozen_req`）：表单勾选是客户端本地态，重渲染会冲掉，故首帧渲染后冻结，直到 core 清除 pending_question
- 响应格式：`[{question: idx, selectedOption/selectedOptions: ...}, ...]`
- 旧的逐题 callback（`hook_question`/`hook_question_toggle`/`hook_question_confirm`）路由与 handler 仍保留作在途旧卡片的向后兼容，新卡片不再发出这些 action

### 会话列表来源标识（菜单卡片）

- 列表每个会话按 `list_active_sessions()` 的 `tmux` 字段标来源：
  - `tmux=True` → 「📟 终端会话 · 关闭会断终端」（橙色，`cla` 启动、有 `rc-` tmux，飞书只是连进来）
  - `tmux=False` → 「💬 飞书会话 · 可直接关」（蓝色，飞书 `start_new_session` 裸进程）
- 关闭确认文案据此区分：终端会话额外警告"会同时断开终端那侧的 claude/codex"

## 开发须知

- **系统要求：** macOS/Linux，需已安装 `uv`、`tmux`、`claude`/`codex` CLI
- **飞书配置：** `~/.agents-remote/.env`（`FEISHU_APP_ID` + `FEISHU_APP_SECRET`）
- **运行时文件：** `/tmp/agents-remote/`（.mq / .sock / .pid / _hooks/）
- **tmux 会话前缀：** `rc-`
- **语言：** 代码注释和用户交互均使用中文
- **server 端逻辑（解析/hooks/PTY）的修改在 `agents-remote-core` 仓库**

### 变更同步规则

修改本项目的卡片交互、命令行为、新增功能时，同步更新 `CLAUDE.md`。
server 端（解析规则、hook 注入等）的文档在 `agents-remote-core/CLAUDE.md`。
