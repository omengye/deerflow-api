# DeerFlow ACP Portable

这是 DeerFlow ACP 的 Windows x64 便携版。解压 ZIP 后无需安装。

## 首次使用

1. 运行 `deerflow-config.exe`。
2. 在“模型”页面配置 Provider、模型 ID、Base URL 和 API Key。
3. 按需配置 Custom Agent、Skills 与 Runtime / ACP 参数。
4. 点击“保存配置”，再从概览页启动 Daemon。

首次启动会自动创建 `user-data` 下的配置、数据、Skill、日志、备份和 ACP runtime 目录。所有用户数据都留在解压目录中；移动整个目录后仍可使用。

## ACP 客户端配置

DeerFlow 使用标准 ACP stdio 协议。将 ACP Client 的启动命令设置为解压目录下 `deerflow-acp.exe` 的**绝对路径**，参数保持为空。Bridge 会自动发现并使用：

- `user-data/config/config.yaml`
- `runtime/python.exe`
- `user-data/runtime/acp`

不要把 `deerflow-config.exe`、`--start-daemon`、`--status` 或 `--gateway` 配置为 ACP Agent 命令；客户端需要启动的是不带模式参数的 `deerflow-acp.exe`。默认 Bridge 会在需要时自动启动 Daemon，并通过 stdio 与客户端交换 ACP JSON-RPC。

### Zed

打开 Zed 的 `settings.json`，把下面的 `agent_servers` 合并到现有配置中。将示例路径替换为实际解压路径；JSON 中的 Windows 反斜杠必须写成 `\\`：

```json
{
  "agent_servers": {
    "DeerFlow Local": {
      "type": "custom",
      "command": "D:\\Apps\\DeerFlow\\deerflow-acp.exe",
      "args": []
    }
  }
}
```

保存设置后，在 Zed 的 Agent 面板中选择 `DeerFlow Local`。Zed 会把当前项目目录作为 ACP session 的工作目录传给 DeerFlow；无需在参数中配置项目路径。多个 Zed 窗口可以指向同一份便携目录并共享常驻 Daemon，但每个窗口仍使用独立的 ACP session。

### 其他 ACP Client

在 Waku 或其他支持自定义 ACP Agent 的客户端中使用以下设置：

- Transport / 通信方式：`stdio`
- Command / Program：`D:\Apps\DeerFlow\deerflow-acp.exe`（替换为实际绝对路径）
- Arguments / Args：留空
- Working directory：由客户端使用当前项目目录传入
- Environment：通常留空

如果客户端只接受一条命令行，可填写带引号的完整路径，例如：

```text
"D:\Apps\DeerFlow\deerflow-acp.exe"
```

不要通过 `cmd /c` 或 PowerShell 包装该命令，否则包装层可能干扰 ACP stdio、进程退出和取消信号。

### 启动与排障

第一次连接前应先运行一次 `deerflow-config.exe` 并保存模型配置。Daemon 可以由 ACP Client 首次连接时自动启动，也可以从配置工具的概览页提前启动。

在解压目录打开 PowerShell，可以检查或控制 Daemon：

```powershell
.\deerflow-acp.exe --status
.\deerflow-acp.exe --start-daemon
.\deerflow-acp.exe --stop-daemon
```

如果客户端连接失败，请依次检查：

1. 客户端中的 `command` 是否为当前解压目录下 EXE 的绝对路径。
2. 是否已通过 `deerflow-config.exe` 保存有效的模型配置。
3. `user-data/runtime/acp/daemon.log` 中是否有启动或模型加载错误。
4. 移动便携目录后，是否同步更新了客户端中的绝对路径。

本工具不会自动修改或测试 Waku、Zed 等 ACP Client 的配置，客户端侧连通性需要手动验证。

## Skill 自进化

“Skills”页面可以启用 Self Improving，并配置 Review / Auto Patch 模式、生成/审核/评估模型、自动发现阈值、候选大小限制和自动回滚阈值。Auto Patch 的创建、支持文件、脚本和删除能力始终由安全锁禁用。

“Skills”页面可以查看待审批 Proposal 的详情、Diff、安全扫描和评估结果，并执行批准发布或拒绝。Signal、Probation、归档和完整历史仍在 DeerFlow Admin 页面中管理。

## Subagents

“Agent”页面包含与 Admin 一致的 Subagents 全局开关、默认超时、默认最大轮次和模型分配，也可以直接编辑 `agents` 与 `custom_agents` 高级 JSON。Subagent 全局开关和“Runtime / ACP”页面的会话级 Subagents 开关需要同时启用。

页面下半部分的 Custom Agents 是可直接作为 ACP 主 Agent 使用的独立 Agent 目录，与任务执行期间由主 Agent 调用的 Subagent 配置相互独立。

## Sandbox 与本地工具

“Sandbox / Tools”页面可以配置 Sandbox Provider、Host Bash/Host Tools 安全开关、Tool Groups、Tools，以及 ACP 会话的工具 Allowlist/Denylist。Local 和 WSL Provider 共享宿主机文件系统，不应当作隔离边界；Host 权限仅适用于完全可信的本地环境。

Sandbox 与 Tool JSON 中的字面量密钥会显示为 `__DEERFLOW_REDACTED__`。保留占位符会继续使用原值，输入新值则会替换原值。工具名称必须唯一，并且每个工具必须引用已配置的 Tool Group。

## 数据与恢复

- 配置：`user-data/config/config.yaml`
- Custom Agents：`user-data/data/deerflow/agents`
- Skills：`user-data/skills`
- Daemon 日志与端点：`user-data/runtime/acp`
- 配置备份：`user-data/backups`

模型的字面量密钥在界面读取时会被脱敏。保存时密钥输入框留空会保留原值；只有勾选“清除已保存的 API Key”才会删除它。
