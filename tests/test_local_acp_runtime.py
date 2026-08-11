from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from acp import schema

import deerflow.acp.runtime as runtime_module
from deerflow.acp.client_mcp import normalize_client_mcp_servers
from deerflow.acp.config import LocalACPConfig
from deerflow.acp.runtime import LocalACPRuntime
from deerflow.acp.session_store import LocalACPSession
from deerflow.client import DeerFlowClient


@pytest.mark.asyncio
async def test_runtime_warmup_builds_and_reuses_default_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances: list[FakeClient] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.warmup_calls = 0
            instances.append(self)

        def warmup(self) -> None:
            self.warmup_calls += 1

    monkeypatch.setattr(runtime_module, "DeerFlowClient", FakeClient)
    config = LocalACPConfig(
        config_path=tmp_path / "config.yaml",
        checkpointer_path=tmp_path / "checkpoints.db",
        session_store_path=tmp_path / "sessions.db",
        model_name="test-model",
        thinking_enabled=False,
        subagent_enabled=True,
        plan_mode=False,
        max_concurrent_subagents=3,
        recursion_limit=25,
        agent_name="test-agent",
    )
    runtime = LocalACPRuntime(config)
    checkpointer = object()
    runtime._checkpointer = checkpointer

    await runtime.warmup()
    await runtime.warmup()

    assert len(instances) == 1
    assert instances[0].warmup_calls == 2
    kwargs = instances[0].kwargs
    assert kwargs["config_path"] == str(config.config_path)
    assert kwargs["checkpointer"] is checkpointer
    assert kwargs["model_name"] == "test-model"
    assert kwargs["thinking_enabled"] is False
    assert kwargs["subagent_enabled"] is True
    assert kwargs["max_concurrent_subagents"] == 3
    assert kwargs["agent_name"] == "test-agent"
    assert "task" not in kwargs["excluded_tool_names"]
    assert "invoke_acp_agent" in kwargs["excluded_tool_names"]
    assert "create_scheduled_task" in kwargs["excluded_tool_names"]
    assert kwargs["system_prompt_overlay"]
    assert (
        "further delegation is unavailable" in kwargs["subagent_system_prompt_overlay"]
    )
    assert len(kwargs["middlewares"]) == 1
    assert kwargs["subagent_middlewares"] == kwargs["middlewares"]


@pytest.mark.asyncio
async def test_runtime_builds_a_new_client_after_session_model_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances: list[FakeClient] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            instances.append(self)

    monkeypatch.setattr(runtime_module, "DeerFlowClient", FakeClient)
    config = LocalACPConfig(
        config_path=tmp_path / "config.yaml",
        checkpointer_path=tmp_path / "checkpoints.db",
        session_store_path=tmp_path / "sessions.db",
    )
    runtime = LocalACPRuntime(config)
    runtime._checkpointer = object()
    session = LocalACPSession(
        session_id="model-switch-session",
        cwd=str(tmp_path),
        title=None,
        updated_at="",
        model_name="model-a",
        thinking_enabled=True,
        subagent_enabled=False,
        plan_mode=True,
        max_concurrent_subagents=1,
        recursion_limit=200,
    )

    first = await runtime._client_for(session)
    session.model_name = "model-b"
    second = await runtime._client_for(session)

    assert first is not second
    assert [instance.kwargs["model_name"] for instance in instances] == [
        "model-a",
        "model-b",
    ]


@pytest.mark.asyncio
async def test_runtime_includes_bash_only_when_explicitly_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances: list[FakeClient] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            instances.append(self)

        def warmup(self) -> None:
            pass

    monkeypatch.setattr(runtime_module, "DeerFlowClient", FakeClient)
    config = LocalACPConfig(
        config_path=tmp_path / "config.yaml",
        checkpointer_path=tmp_path / "checkpoints.db",
        session_store_path=tmp_path / "sessions.db",
        enable_bash=True,
    )
    runtime = LocalACPRuntime(config)
    runtime._checkpointer = object()

    await runtime.warmup()

    assert instances[0].kwargs["excluded_tool_names"] == {
        "invoke_acp_agent",
        "task",
        "task_status",
        "create_scheduled_task",
        "list_scheduled_tasks",
        "set_scheduled_task_enabled",
        "delete_scheduled_task",
        "list_scheduled_task_runs",
    }


@pytest.mark.asyncio
async def test_runtime_injects_client_mcp_tools_into_a_session_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances: list[FakeClient] = []
    loaded_configs: list[Any] = []
    injected_tool = type("FakeTool", (), {"name": "codeg_read_file"})()

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            instances.append(self)

    async def fake_get_mcp_tools(config: Any) -> list[Any]:
        loaded_configs.append(config)
        return [injected_tool]

    monkeypatch.setattr(runtime_module, "DeerFlowClient", FakeClient)
    monkeypatch.setattr("deerflow.mcp.tools.get_mcp_tools", fake_get_mcp_tools)
    config = LocalACPConfig(
        config_path=tmp_path / "config.yaml",
        checkpointer_path=tmp_path / "checkpoints.db",
        session_store_path=tmp_path / "sessions.db",
        accept_client_mcp_servers=True,
    )
    runtime = LocalACPRuntime(config)
    runtime._checkpointer = object()
    session = LocalACPSession(
        session_id="session-with-codeg-mcp",
        cwd=str(tmp_path),
        title=None,
        updated_at="",
        model_name=None,
        thinking_enabled=True,
        subagent_enabled=True,
        plan_mode=True,
        max_concurrent_subagents=3,
        recursion_limit=200,
    )
    binding = normalize_client_mcp_servers(
        [
            schema.McpServerStdio(
                name="codeg",
                command="codeg-mcp",
                args=[],
                env=[],
            )
        ],
        enabled=True,
    )
    assert binding is not None

    await runtime.bind_client_mcp(session.session_id, binding)
    client = await runtime._client_for(session)

    assert client.kwargs["additional_mcp_tools"] == [injected_tool]
    assert client.kwargs["subagent_enabled"] is False
    assert client.kwargs["max_concurrent_subagents"] == 1
    assert loaded_configs == [binding.extensions_config]
    assert any(key[0] == "client-mcp" for key in runtime._clients)

    await runtime.release_client_mcp(session.session_id)
    assert session.session_id not in runtime._client_mcp_bindings
    assert not any(key[0] == "client-mcp" for key in runtime._clients)


@pytest.mark.asyncio
async def test_runtime_retries_failed_client_mcp_scope_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    class FakeSessionPool:
        async def close_scope(self, session_id: str) -> None:
            attempts.append(session_id)
            if len(attempts) == 1:
                raise RuntimeError("temporary cleanup failure")

    monkeypatch.setattr(
        "deerflow.mcp.session_pool.get_session_pool", lambda: FakeSessionPool()
    )
    runtime = LocalACPRuntime(
        LocalACPConfig(
            config_path=tmp_path / "config.yaml",
            checkpointer_path=tmp_path / "checkpoints.db",
            session_store_path=tmp_path / "sessions.db",
            accept_client_mcp_servers=True,
        )
    )
    binding = normalize_client_mcp_servers(
        [schema.McpServerStdio(name="retry-mcp", command="retry-mcp", args=[], env=[])],
        enabled=True,
    )
    assert binding is not None
    session_id = "cleanup-retry-session"
    await runtime.bind_client_mcp(session_id, binding)

    with pytest.raises(RuntimeError, match="temporary cleanup failure"):
        await runtime.release_client_mcp(session_id)
    assert session_id in runtime._client_mcp_bindings

    await runtime.release_client_mcp(session_id)
    assert attempts == [session_id, session_id]
    assert session_id not in runtime._client_mcp_bindings


@pytest.mark.asyncio
async def test_runtime_passes_session_cwd_as_workspace_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def astream(self, message: str, **kwargs: Any):
            calls.append({"message": message, **kwargs})
            if False:
                yield None

    monkeypatch.setattr(runtime_module, "DeerFlowClient", FakeClient)
    monkeypatch.setattr(
        "deerflow.config.get_app_config",
        lambda: type(
            "Config",
            (),
            {
                "sandbox": type(
                    "SandboxConfig",
                    (),
                    {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
                )()
            },
        )(),
    )
    config = LocalACPConfig(
        config_path=tmp_path / "config.yaml",
        checkpointer_path=tmp_path / "checkpoints.db",
        session_store_path=tmp_path / "sessions.db",
    )
    runtime = LocalACPRuntime(config)
    runtime._checkpointer = object()
    session = LocalACPSession(
        session_id="workspace-session",
        cwd=str(tmp_path),
        title=None,
        updated_at="",
        model_name=None,
        thinking_enabled=True,
        subagent_enabled=False,
        plan_mode=False,
        max_concurrent_subagents=1,
        recursion_limit=100,
    )

    async for _ in runtime.astream(
        session,
        "inspect files",
        live_event_callback=lambda _event: None,  # type: ignore[arg-type]
    ):
        pass

    assert len(calls) == 1
    assert callable(calls[0].pop("live_event_callback"))
    assert calls == [
        {
            "message": "inspect files",
            "thread_id": "workspace-session",
            "workspace_path": str(tmp_path),
            "user_id": runtime._memory_user_id(session, str(tmp_path)),
        }
    ]


@pytest.mark.asyncio
async def test_runtime_queues_distinct_sessions_and_cancels_waiter_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class FakeClient:
        async def astream(self, message: str, **kwargs: Any):
            del kwargs
            started.append(message)
            if message == "first":
                first_started.set()
                await release_first.wait()
            if False:
                yield None

    monkeypatch.setattr(
        "deerflow.config.get_app_config",
        lambda: SimpleNamespace(
            sandbox=SimpleNamespace(use="deerflow.sandbox.local:LocalSandboxProvider")
        ),
    )
    runtime = LocalACPRuntime(
        LocalACPConfig(
            config_path=tmp_path / "config.yaml",
            checkpointer_path=tmp_path / "checkpoints.db",
            session_store_path=tmp_path / "sessions.db",
            max_active_runs=1,
        )
    )
    fake_client = FakeClient()

    async def client_for(_session: LocalACPSession) -> FakeClient:
        return fake_client

    monkeypatch.setattr(runtime, "_client_for", client_for)

    def session(session_id: str) -> LocalACPSession:
        return LocalACPSession(
            session_id=session_id,
            cwd=str(tmp_path),
            title=None,
            updated_at="",
            model_name=None,
            thinking_enabled=True,
            subagent_enabled=False,
            plan_mode=False,
            max_concurrent_subagents=1,
            recursion_limit=100,
        )

    async def consume(target: LocalACPSession, message: str) -> None:
        async for _ in runtime.astream(
            target,
            message,
            live_event_callback=lambda _event: None,  # type: ignore[arg-type]
        ):
            pass

    first = asyncio.create_task(consume(session("session-a"), "first"))
    await first_started.wait()
    waiting = asyncio.create_task(consume(session("session-b"), "second"))
    await asyncio.sleep(0)
    assert started == ["first"]

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not first.done()

    release_first.set()
    await first
    assert started == ["first"]


@pytest.mark.asyncio
async def test_runtime_rejects_container_provider_for_client_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "deerflow.config.get_app_config",
        lambda: type(
            "Config",
            (),
            {
                "sandbox": type(
                    "SandboxConfig",
                    (),
                    {"use": "deerflow.sandbox.aio:AioSandboxProvider"},
                )()
            },
        )(),
    )
    runtime = LocalACPRuntime(
        LocalACPConfig(
            config_path=tmp_path / "config.yaml",
            checkpointer_path=tmp_path / "checkpoints.db",
            session_store_path=tmp_path / "sessions.db",
        )
    )
    runtime._checkpointer = object()
    session = LocalACPSession(
        session_id="unsupported-workspace",
        cwd=str(tmp_path),
        title=None,
        updated_at="",
        model_name=None,
        thinking_enabled=True,
        subagent_enabled=False,
        plan_mode=False,
        max_concurrent_subagents=1,
        recursion_limit=100,
    )

    with pytest.raises(
        RuntimeError, match="require LocalSandboxProvider or LocalWslProvider"
    ):
        async for _ in runtime.astream(
            session,
            "inspect files",
            live_event_callback=lambda _event: None,  # type: ignore[arg-type]
        ):
            pass


def test_embedded_client_excludes_unsafe_local_acp_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "deerflow.tools.get_available_tools",
        lambda **_kwargs: [
            SimpleNamespace(name="read_file"),
            SimpleNamespace(name="bash"),
            SimpleNamespace(name="task"),
            SimpleNamespace(name="task_status"),
            SimpleNamespace(name="invoke_acp_agent"),
        ],
    )
    client = object.__new__(DeerFlowClient)
    client._additional_mcp_tools = []
    client._excluded_tool_names = frozenset(
        {"bash", "task", "task_status", "invoke_acp_agent"}
    )
    client._tool_groups = None
    client._allowed_tool_names = None

    tools = client._get_tools(model_name=None, subagent_enabled=True)

    assert [tool.name for tool in tools] == ["read_file"]
