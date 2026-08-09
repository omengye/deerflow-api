from __future__ import annotations

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
        max_concurrent_subagents=7,
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
    assert instances[0].kwargs == {
        "config_path": str(config.config_path),
        "checkpointer": checkpointer,
        "model_name": "test-model",
        "thinking_enabled": False,
        "subagent_enabled": False,
        "plan_mode": False,
        "max_concurrent_subagents": 1,
        "recursion_limit": 25,
        "agent_name": "test-agent",
        "checkpoint_channel_mode": "full",
        "excluded_tool_names": {"bash", "invoke_acp_agent", "task", "task_status"},
    }


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
        }
    ]


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

    with pytest.raises(RuntimeError, match="require LocalSandboxProvider or LocalWslProvider"):
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

    tools = client._get_tools(model_name=None, subagent_enabled=True)

    assert [tool.name for tool in tools] == ["read_file"]
