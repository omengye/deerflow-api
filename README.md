# DeerFlow API Service

基于 [DeerFlow harness](../deer-flow/) 构建的 FastAPI 服务，将 Agent harness 能力暴露为 REST + SSE API。

## 快速启动

```bash
cd deerflow-api

# 1. 安装依赖（uv 管理）
uv sync --frozen --inexact

# 2. 编辑 config.yaml，填入你的 API Key
# 默认使用 DashScope（通义千问），替换 YOUR_DASHSCOPE_API_KEY

# 3. 启动
./start.sh
# 停止
./stop.sh
# 或手动启动：
uv run uvicorn app:app --host 0.0.0.0 --port 8000 --app-dir app
```

## 配置方式

运行参数优先写入 `config.yaml` 的 `api:` 段，例如监听地址、端口、CORS、上传限制、默认 agent 模式、并发子 agent 数和 DeerFlow 数据目录。旧的 `DEER_FLOW_*`、`HOST`、`PORT` 环境变量仍作为兼容 fallback，但常规部署不再需要 `.env`。

```yaml
api:
  host: 0.0.0.0
  port: 8000
  deerflow_home: ./data/deerflow
  plan_mode: true
  subagent_enabled: true
  max_concurrent_subagents: 3
  chat_request_timeout: 600
  cors_allow_origins:
    - http://localhost:3000
    - http://127.0.0.1:3000
  max_upload_size_mb: 25
  max_uploads_per_request: 10
```

Tracing 也放在 `config.yaml.tracing` 下；密钥可以直接写入 YAML，也可以用 `$ENV_NAME` 占位符继续从环境变量读取：

```yaml
tracing:
  langfuse:
    enabled: false
    public_key: $LANGFUSE_PUBLIC_KEY
    secret_key: $LANGFUSE_SECRET_KEY
    host: https://cloud.langfuse.com
```

仍建议保留在环境变量中的只有进程级或系统级凭据/变量，例如 `CLAUDE_CODE_OAUTH_TOKEN`、`CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`、`ANTHROPIC_AUTH_TOKEN`、`HOME`、`SystemRoot` 等。

## Checkpoint 与长期记忆

本分支已同步字节 DeerFlow 近期的 checkpoint / memory 优化思路：checkpoint 支持 `full`、`delta` 双模式和可配置快照频率；delta 历史可使用进程内 LRU 或 Redis 缓存。模式与快照频率都是进程级冻结配置，修改后必须重启所有共享同一 checkpoint 数据库的进程。`full -> delta` 可直接读取；`delta -> full` 会 fail-closed，避免把 delta sentinel 误当成空状态。

长期记忆通过 `MemoryManager` 接口选择后端。默认 `deermem` 兼容原有 JSON + FTS5 行为；`mem0` 使用 HTTP API，API Key 只从 `backend_config.api_key_env` 指定的环境变量读取。mem0 不会自动迁移 DeerMem 数据。

记忆支持 `middleware` 自动召回和 `tool` 按需召回两种模式；tool 模式会注册 `memory_search`，运行时 `user_id`、`agent_name`、`thread_id` 会分别映射到 mem0 的用户、Agent 和 run 作用域。

启用 API 鉴权后，可在 `/management/` 的“长期记忆”区域编辑完整 Memory 配置，先执行校验/后端测试，再“保存并应用”。mem0 的实际 API Key 不会写入或回显到管理页面，必须预先放入服务进程环境变量；页面只保存环境变量名。切换 DeerMem、mem0 或自定义后端不会自动迁移已有数据。

Linux 生产部署时，运行用户必须对 `config.yaml` 和 DeerMem 数据所在目录有写权限，以便创建 `.config.yaml.lock`、`.memory.json.lock` 及同目录原子临时文件。单机多 worker 必须共享相同的配置、数据和锁文件目录；由于热重载只作用于接收管理请求的进程，多 worker 部署在保存后仍应滚动重启全部 worker。上线前请备份 `config.yaml` 和 DeerMem 数据文件，并用 `/health/ready` 与管理页“测试后端”复核。

完整配置、迁移规则和故障策略见 [Checkpoint 与记忆优化说明](docs/checkpoint-memory-optimizations.md)，可直接复制的 YAML 见 [config.example.yaml](config.example.yaml)。

## ACP Agent 对接（Codex / Claude Code）

项目支持作为 **ACP client** 调用外部 agent。配置 `config.yaml` 的 `acp_agents:` 后，DeerFlow 会自动暴露内置工具 `invoke_acp_agent`，主 agent 可以把编码、审查、重构等任务交给外部 ACP agent 执行。

> ACP 这里指 Agent Client Protocol。被启动的外部命令必须实现 ACP 协议；裸 `codex` 或 `claude` CLI 通常不能直接作为 ACP agent 使用，需要对应的 ACP adapter。

示例：

```yaml
acp_agents:
  codex:
    command: codex-acp
    args: []
    description: "Codex coding agent via ACP"
    model: null
    auto_approve_permissions: false

  claude_code:
    command: claude-agent-acp
    args: []
    description: "Claude Code coding agent via ACP"
    model: null
    auto_approve_permissions: false
```

常见 adapter 安装方式：

```bash
# Codex ACP adapter
npm install -g @zed-industries/codex-acp

# Claude Code ACP adapter
npm install -g @agentclientprotocol/claude-agent-acp
```

运行机制与注意事项：

- `command`/`args` 会作为子进程启动，并通过 ACP `initialize -> new_session -> prompt` 流程调用。
- 每个会话线程有独立 ACP 工作目录；外部 agent 产物会映射到 DeerFlow 的 `/mnt/acp-workspace/`。
- `auto_approve_permissions` 默认应保持 `false`。改为 `true` 后，DeerFlow 会自动批准 ACP agent 的权限请求，适合受信环境，不适合开放服务。
- `acp_agents` 是“把 Codex/Claude Code 当外部 agent 调用”；项目也支持把 Codex/Claude Code 凭据配置成主模型 provider，这两种能力互相独立。

## Windows 沙箱（WSL 模式）

Windows 上 `LocalSandboxProvider` 会回退到 PowerShell/cmd.exe，与上游 agent prompts 的 bash 语义不兼容；同时开启 `allow_host_bash: true` 后 LLM 命令直接在 Windows 主机权限下执行，没有任何隔离。

为此提供 `LocalWslProvider`：**bash 跑进 WSL2，文件 I/O 仍在 Windows 上**——agent prompt 原生可用，进程层面有 VM 隔离，性能与现有线程目录全部沿用。

### 前提
- Windows 10/11 + 已安装 WSL2 与一个 Linux distro（推荐 `Ubuntu-22.04`）
- WSL 版本 ≥ 0.64.0（启用 `WSL_UTF8` 环境变量支持，避免输出乱码）
- 用 `wsl -l -v` 确认 distro 存在且为 Version 2

### 启用方式
编辑 `config.yaml` 的 `sandbox:` 段：

```yaml
sandbox:
  use: deerflow.sandbox.local:LocalWslProvider   # 也可以写短别名 'wsl'
  wsl_distro: Ubuntu-22.04       # 留空则使用默认 distro
  wsl_user: null                 # 留空则使用 distro 的默认用户
  wsl_shell: bash                # 也可以填 zsh
  wsl_mount_prefix: /mnt         # 与 /etc/wsl.conf 的 automount.root 一致
```

不需要再开 `allow_host_bash`——非 local provider 默认放行 bash 工具与 bash 子代理。

### 路径映射
agent 看到的虚拟路径 → Windows 主机路径 → WSL 路径自动来回翻译：

| Agent 视角 | Windows 实际位置 | WSL 视角 |
|---|---|---|
| `/mnt/user-data/workspace/x.py` | `D:\Tools\deerflow-api\data\threads\<tid>\user-data\workspace\x.py` | `/mnt/d/Tools/deerflow-api/data/threads/<tid>/user-data/workspace/x.py` |
| `/mnt/skills/<name>` | `D:\Tools\deerflow-api\skills\<name>` | `/mnt/d/Tools/deerflow-api/skills/<name>` |

bash 输出里的 `/mnt/<drive>/...` 会被自动还原成虚拟路径再返回给 agent，主机绝对路径不会泄漏。

### 安全说明
- **不是真"安全沙箱"**：WSL2 默认能读写 `/mnt/c/...`、能访问 `%USERPROFILE%`。比直跑 PowerShell 安全一个量级，但弱于 Docker/AioSandbox。
- 启动失败有明确报错：未装 WSL、`wsl.exe` 不在 PATH、distro 未注册、非 Windows 主机都会在启动期硬失败。
- 真要强隔离仍需后续移植 `AioSandboxProvider` + Docker Desktop。

### 故障排查
- `WslUnavailableError: wsl.exe was not found` → 安装 WSL：`wsl --install`
- `WslDistroNotFoundError: ...is not registered` → 检查 `wsl -l -q` 输出，确认配置的 `wsl_distro` 拼写
- bash 输出乱码 → 升级 WSL：`wsl --update`（需 ≥ 0.64.0 以支持 `WSL_UTF8`）
- `bash` 工具返回 `Host bash execution is disabled` → 确认 `sandbox.use` 真的指向 `wsl` 而不是 local

## 项目结构

```
deerflow-api/
├── app/
│   ├── __init__.py          # FastAPI app + lifespan
│   ├── config.py            # 服务配置
│   ├── dependencies.py      # ClientManager 单例
│   ├── schemas.py           # Pydantic 请求/响应模型
│   └── routers/
│       ├── chat.py          # POST /api/chat + /api/chat/stream（SSE）
│       ├── threads.py       # 线程管理
│       ├── models.py        # 模型列表
│       ├── skills.py        # 技能管理
│       ├── mcp.py           # MCP 配置
│       └── uploads.py       # 文件上传/下载
├── config.yaml              # DeerFlow harness 配置
├── config.example.yaml      # 配置模板
├── skills/public/           # 21 个内置 skills
├── start.sh                 # 启动脚本
├── stop.sh                  # 停止脚本
└── pyproject.toml           # uv 项目定义
```

## 核心设计

- **uv 管理依赖** — 使用 `uv sync` 安装，虚拟环境在 `.venv/`
- **ClientManager 单例** — 全局共享 `DeerFlowClient`，按配置 key 缓存 agent
- **Lazy Agent** — 内部 agent 延迟创建，首次调用时才初始化
- **默认 Ultra 能力** — 默认启用 `plan_mode` 与 `subagent_enabled`，支持自主规划和最多 3 个子 agent 并行任务
- **SQLite 持久化** — 默认 SQLite checkpointer，重启不丢失对话
- **精简依赖** — 移除了 markitdown（onnxruntime 不兼容 Python 3.14），社区工具作为可选依赖

## 依赖精简

从原 harness 移除了：
- ❌ `markitdown[all,xlsx]` → 改用 markitdown 基础版（可选依赖）
- ❌ `kubernetes` → 移至可选依赖 `[sandbox-docker]`
- ❌ `tavily-python`, `firecrawl-py`, `exa-py`, `ddgs` → 社区工具已移除

当前核心依赖约 **126 个包**，虚拟环境 **~395MB**。

## API 端点

启动后访问 http://localhost:8000/docs 查看 Swagger UI

### 对话
| 方法 | 端点 | 说明 |
|------|------|------|
| `POST` | `/api/chat` | 同步对话 |
| `POST` | `/api/chat/stream` | SSE 流式输出 |

请求体支持运行模式覆盖：

```json
{
  "message": "分析这个项目并给出改造计划",
  "plan_mode": true,
  "subagent_enabled": true,
  "max_concurrent_subagents": 3
}
```

- `plan_mode`: 启用 `write_todos` 规划/追踪能力
- `subagent_enabled`: 启用 `task` 工具，由主 agent 分派子 agent
- `max_concurrent_subagents`: 每轮最多并行子 agent 数，范围 2-4，默认 3

### 线程 / 模型 / 技能 / MCP / 文件
详见 `/docs` 交互文档
