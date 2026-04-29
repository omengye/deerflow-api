# DeerFlow API Service

基于 [DeerFlow harness](../deer-flow/) 构建的 FastAPI 服务，将 Agent harness 能力暴露为 REST + SSE API。

## 快速启动

```bash
cd deerflow-api

# 1. 安装依赖（uv 管理）
uv sync

# 2. 编辑 config.yaml，填入你的 API Key
# 默认使用 DashScope（通义千问），替换 YOUR_DASHSCOPE_API_KEY

# 3. 启动
./start.sh
# 或手动启动：
uv run uvicorn app:app --host 0.0.0.0 --port 8000 --app-dir app
```

## 环境变量

| 变量 | 说明 | 默认 |
|------|------|------|
| `DEER_FLOW_CONFIG_PATH` | config.yaml 路径 | `./config.yaml` |
| `DEER_FLOW_HOME` | DeerFlow 记忆、线程工作区等持久状态目录 | `./data/deerflow` |
| `DEER_FLOW_PLAN_MODE` | 默认是否启用自主规划 | `true` |
| `DEER_FLOW_SUBAGENT_ENABLED` | 默认是否启用多 agent | `true` |
| `DEER_FLOW_MAX_CONCURRENT_SUBAGENTS` | 默认并行子 agent 数 | `3` |
| `DEER_FLOW_CHAT_TIMEOUT` | `/api/chat` 单次请求超时（秒） | `600` |
| `DEER_FLOW_CORS_ORIGINS` | 允许的 CORS 来源（逗号分隔） | `http://localhost:3000,http://127.0.0.1:3000` |
| `DEER_FLOW_CORS_ALLOW_CREDENTIALS` | CORS 是否携带凭据 | `false` |
| `DEER_FLOW_MAX_UPLOAD_SIZE_MB` | 单个上传文件大小上限（MB） | `25` |
| `DEER_FLOW_MAX_UPLOADS_PER_REQUEST` | 单次请求最多上传文件数 | `10` |
| `DEER_FLOW_ALLOWED_UPLOAD_EXTENSIONS` | 允许的扩展名（逗号分隔，留空 = 全部允许） | （空） |
| `HOST` | 监听地址 | `0.0.0.0` |
| `PORT` | 服务端口 | `8000` |
| `RELOAD` | 设置即启用热重载 | 未设置 |

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
