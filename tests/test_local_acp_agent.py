from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import acp
import pytest
from acp import RequestError, schema

from deerflow.acp.agent import DeerFlowACPAgent
from deerflow.acp.config import LocalACPConfig
from deerflow.acp.event_mapper import ACPEventMapper
from deerflow.acp.session_store import LocalACPSessionStore


class FakeConnection:
    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))


class FakeRuntime:
    def __init__(self, events: list[Any] | None = None) -> None:
        self.events = events or []
        self.history_messages: list[dict[str, Any]] = []
        self.started = asyncio.Event()
        self.block = False
        self.client_mcp_bindings: dict[str, Any] = {}
        self.released_client_mcp: list[str] = []

    async def bind_client_mcp(self, session_id: str, binding: Any) -> None:
        if binding is None:
            self.client_mcp_bindings.pop(session_id, None)
        else:
            self.client_mcp_bindings[session_id] = binding

    async def release_client_mcp(self, session_id: str) -> None:
        self.client_mcp_bindings.pop(session_id, None)
        self.released_client_mcp.append(session_id)

    async def astream(self, session: Any, message: str, *, live_event_callback: Any):
        del session, message
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        for event in self.events:
            if isinstance(event, dict) and event.get("live"):
                await live_event_callback(event["data"])
            else:
                yield event

    async def history(self, session_id: str) -> list[dict[str, Any]]:
        del session_id
        return self.history_messages


def make_config(tmp_path: Path, **overrides: Any) -> LocalACPConfig:
    values: dict[str, Any] = {
        "config_path": tmp_path / "config.yaml",
        "checkpointer_path": tmp_path / "checkpoints.db",
        "session_store_path": tmp_path / "sessions.db",
        "run_timeout_seconds": 10,
        "session_page_size": 2,
    }
    values.update(overrides)
    return LocalACPConfig(**values)


def test_config_paths_do_not_depend_on_client_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "deerflow-config"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        """
api:
  data_dir: ./state
  deerflow_home: ./state/home
local_acp:
  checkpointer_path: ./state/checkpoints.db
  session_store_path: ./state/sessions.db
  thinking_enabled: false
  accept_client_mcp_servers: true
""",
        encoding="utf-8",
    )
    client_workspace = tmp_path / "client-workspace"
    client_workspace.mkdir()
    monkeypatch.chdir(client_workspace)
    monkeypatch.delenv("DEER_FLOW_CONFIG_PATH", raising=False)
    monkeypatch.delenv("DEER_FLOW_HOME", raising=False)

    config = LocalACPConfig.from_file(str(config_path))

    assert config.checkpointer_path == config_dir / "state" / "checkpoints.db"
    assert config.session_store_path == config_dir / "state" / "sessions.db"
    assert config.deerflow_home == config_dir / "state" / "home"
    assert config.thinking_enabled is False
    assert config.accept_client_mcp_servers is True
    assert "DEER_FLOW_CONFIG_PATH" not in os.environ


@pytest.fixture
def store(tmp_path: Path):
    result = LocalACPSessionStore(tmp_path / "sessions.db")
    result.setup()
    try:
        yield result
    finally:
        result.close()


@pytest.mark.asyncio
async def test_initialize_advertises_text_only_stable_capabilities(
    tmp_path: Path,
    store: LocalACPSessionStore,
) -> None:
    agent = DeerFlowACPAgent(make_config(tmp_path), store, FakeRuntime())

    response = await agent.initialize(protocol_version=1)

    assert response.protocol_version == 1
    assert response.agent_capabilities.load_session is True
    assert response.agent_capabilities.prompt_capabilities.image is False
    assert response.agent_capabilities.prompt_capabilities.audio is False
    assert response.agent_capabilities.prompt_capabilities.embedded_context is False
    assert response.agent_capabilities.mcp_capabilities.http is False
    assert response.agent_capabilities.mcp_capabilities.sse is False
    assert response.agent_capabilities.session_capabilities.list is not None
    assert response.agent_capabilities.session_capabilities.resume is None
    assert response.agent_capabilities.session_capabilities.fork is None
    assert response.auth_methods == []


@pytest.mark.asyncio
async def test_new_session_rejects_client_directories_and_mcp(
    tmp_path: Path,
    store: LocalACPSessionStore,
) -> None:
    agent = DeerFlowACPAgent(make_config(tmp_path), store, FakeRuntime())

    with pytest.raises(RequestError) as directories_error:
        await agent.new_session(cwd=str(tmp_path), additional_directories=[str(tmp_path / "other")])
    assert directories_error.value.code == -32602

    with pytest.raises(RequestError) as mcp_error:
        await agent.new_session(cwd=str(tmp_path), mcp_servers=[object()])
    assert mcp_error.value.code == -32602

    response = await agent.new_session(cwd=str(tmp_path), mcp_servers=[])
    session = await store.get(response.session_id)
    assert session is not None
    assert session.cwd == str(tmp_path)
    assert response.modes.current_mode_id == "plan"
    assert {option.id for option in response.config_options or []} == {
        "thinking_enabled",
    }
    assert all(option.type == "select" for option in response.config_options or [])
    assert all(
        {item.value for item in option.options} == {"on", "off"}
        for option in response.config_options or []
    )


@pytest.mark.asyncio
async def test_session_cwd_must_be_an_existing_absolute_directory(
    tmp_path: Path,
    store: LocalACPSessionStore,
) -> None:
    agent = DeerFlowACPAgent(make_config(tmp_path), store, FakeRuntime())
    regular_file = tmp_path / "not-a-directory.txt"
    regular_file.write_text("x", encoding="utf-8")

    for invalid_cwd in (
        "relative/project",
        str(tmp_path / "missing"),
        str(regular_file),
    ):
        with pytest.raises(RequestError) as error:
            await agent.new_session(cwd=invalid_cwd, mcp_servers=[])
        assert error.value.code == -32602


@pytest.mark.asyncio
async def test_load_session_rejects_a_different_workspace(
    tmp_path: Path,
    store: LocalACPSessionStore,
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    agent = DeerFlowACPAgent(make_config(tmp_path), store, FakeRuntime())
    created = await agent.new_session(cwd=str(tmp_path), mcp_servers=[])

    with pytest.raises(RequestError) as error:
        await agent.load_session(
            cwd=str(other),
            session_id=created.session_id,
            mcp_servers=[],
        )

    assert error.value.code == -32602
    assert "does not match cwd" in str(error.value.data)


@pytest.mark.asyncio
async def test_load_session_canonicalizes_a_legacy_stored_workspace(
    tmp_path: Path,
    store: LocalACPSessionStore,
) -> None:
    child = tmp_path / "child"
    child.mkdir()
    agent = DeerFlowACPAgent(make_config(tmp_path), store, FakeRuntime())
    created = await store.create(
        cwd=str(child / ".."),
        defaults=agent._defaults(),
    )

    await agent.load_session(
        cwd=str(tmp_path),
        session_id=created.session_id,
        mcp_servers=[],
    )

    loaded = await store.get(created.session_id)
    assert loaded is not None
    assert loaded.cwd == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_list_sessions_normalizes_cwd_before_filtering(
    tmp_path: Path,
    store: LocalACPSessionStore,
) -> None:
    agent = DeerFlowACPAgent(make_config(tmp_path), store, FakeRuntime())
    created = await agent.new_session(cwd=str(tmp_path), mcp_servers=[])

    listed = await agent.list_sessions(cwd=str(tmp_path) + os.sep + ".")

    assert [item.session_id for item in listed.sessions] == [created.session_id]


@pytest.mark.asyncio
async def test_trusted_stdio_client_mcp_is_session_scoped_and_released(
    tmp_path: Path,
    store: LocalACPSessionStore,
) -> None:
    runtime = FakeRuntime()
    agent = DeerFlowACPAgent(
        make_config(tmp_path, accept_client_mcp_servers=True),
        store,
        runtime,
    )
    server = schema.McpServerStdio(
        name="codeg",
        command="codeg-mcp",
        args=["--stdio"],
        env=[schema.EnvVariable(name="CODEG_SESSION", value="local")],
    )

    created = await agent.new_session(
        cwd=str(tmp_path),
        mcp_servers=[server],
    )

    binding = runtime.client_mcp_bindings[created.session_id]
    config = binding.extensions_config.mcp_servers["codeg"]
    assert config.type == "stdio"
    assert config.command == "codeg-mcp"
    assert config.args == ["--stdio"]
    assert config.env == {"CODEG_SESSION": "local"}

    await agent.shutdown()
    assert created.session_id not in runtime.client_mcp_bindings
    assert runtime.released_client_mcp == [created.session_id]


@pytest.mark.asyncio
async def test_trusted_client_mcp_rejects_non_stdio_transport(
    tmp_path: Path,
    store: LocalACPSessionStore,
) -> None:
    agent = DeerFlowACPAgent(
        make_config(tmp_path, accept_client_mcp_servers=True),
        store,
        FakeRuntime(),
    )
    server = schema.HttpMcpServer(
        type="http",
        name="remote",
        url="https://example.test/mcp",
        headers=[],
    )

    with pytest.raises(RequestError) as error:
        await agent.new_session(cwd=str(tmp_path), mcp_servers=[server])

    assert error.value.code == -32602
    assert "only stdio" in str(error.value.data)


@pytest.mark.asyncio
async def test_session_modes_config_list_and_load_history(
    tmp_path: Path,
    store: LocalACPSessionStore,
) -> None:
    runtime = FakeRuntime()
    runtime.history_messages = [
        {"type": "human", "id": "user-1", "content": "question"},
        {"type": "ai", "id": "agent-1", "content": "answer", "reasoning_content": "thought"},
        {"type": "tool", "id": "tool-1", "content": "hidden"},
    ]
    connection = FakeConnection()
    agent = DeerFlowACPAgent(make_config(tmp_path), store, runtime)
    agent.on_connect(connection)
    created = await agent.new_session(cwd=str(tmp_path), mcp_servers=[])

    await agent.set_session_mode("default", created.session_id)
    options = await agent.set_config_option(
        "thinking_enabled", created.session_id, "off"
    )
    assert (
        next(
            option
            for option in options.config_options
            if option.id == "thinking_enabled"
        ).current_value
        == "off"
    )
    with pytest.raises(RequestError):
        await agent.set_config_option("subagent_enabled", created.session_id, "on")

    listed = await agent.list_sessions(cwd=str(tmp_path))
    assert [item.session_id for item in listed.sessions] == [created.session_id]

    loaded = await agent.load_session(
        cwd=str(tmp_path),
        session_id=created.session_id,
        mcp_servers=[],
    )
    assert loaded.modes.current_mode_id == "default"
    assert [type(update) for _, update in connection.updates] == [
        schema.UserMessageChunk,
        schema.AgentThoughtChunk,
        schema.AgentMessageChunk,
    ]


@pytest.mark.asyncio
async def test_prompt_maps_events_usage_and_title(
    tmp_path: Path,
    store: LocalACPSessionStore,
) -> None:
    events = [
        SimpleNamespace(
            type="messages-tuple",
            data={"type": "ai", "id": "msg-1", "content": "hello", "reasoning_content": "think"},
        ),
        SimpleNamespace(
            type="messages-tuple",
            data={
                "type": "ai",
                "id": "msg-1",
                "content": "",
                "tool_calls": [{"id": "call-1", "name": "web_search", "args": {"q": "x"}}],
            },
        ),
        SimpleNamespace(
            type="messages-tuple",
            data={"type": "tool", "tool_call_id": "call-1", "name": "web_search", "content": "result"},
        ),
        SimpleNamespace(
            type="values",
            data={
                "title": "Task title",
                "todos": [{"content": "Research", "status": "completed"}],
                "artifacts": [],
            },
        ),
        {"live": True, "data": {"type": "task_started", "task_id": "sub-1", "description": "Summarize"}},
        {"live": True, "data": {"type": "token_chunk", "task_id": "sub-1", "content": "working"}},
        {"live": True, "data": {"type": "task_completed", "task_id": "sub-1", "result": "done"}},
        SimpleNamespace(
            type="end",
            data={"usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}},
        ),
    ]
    runtime = FakeRuntime(events)
    connection = FakeConnection()
    agent = DeerFlowACPAgent(make_config(tmp_path), store, runtime)
    agent.on_connect(connection)
    created = await agent.new_session(cwd=str(tmp_path), mcp_servers=[])

    response = await agent.prompt(
        [acp.text_block("do it")],
        created.session_id,
        message_id="11111111-1111-1111-1111-111111111111",
    )

    assert response.stop_reason == "end_turn"
    assert response.usage.total_tokens == 7
    assert response.user_message_id == "11111111-1111-1111-1111-111111111111"
    updates = [update for _, update in connection.updates]
    assert any(isinstance(update, schema.AgentMessageChunk) for update in updates)
    assert any(isinstance(update, schema.AgentThoughtChunk) for update in updates)
    assert any(isinstance(update, schema.AgentPlanUpdate) for update in updates)
    assert any(isinstance(update, schema.SessionInfoUpdate) for update in updates)
    assert sum(isinstance(update, schema.ToolCallStart) for update in updates) == 2
    assert sum(isinstance(update, schema.ToolCallProgress) for update in updates) >= 3
    session = await store.get(created.session_id)
    assert session is not None and session.title == "Task title"


@pytest.mark.asyncio
async def test_prompt_is_text_only_and_one_active_prompt_per_session(
    tmp_path: Path,
    store: LocalACPSessionStore,
) -> None:
    runtime = FakeRuntime()
    runtime.block = True
    connection = FakeConnection()
    agent = DeerFlowACPAgent(make_config(tmp_path), store, runtime)
    agent.on_connect(connection)
    created = await agent.new_session(cwd=str(tmp_path), mcp_servers=[])

    first = asyncio.create_task(agent.prompt([acp.text_block("wait")], created.session_id))
    await runtime.started.wait()
    with pytest.raises(RequestError) as busy_error:
        await agent.prompt([acp.text_block("second")], created.session_id)
    assert busy_error.value.code == -32001

    await agent.cancel(created.session_id)
    response = await first
    assert response.stop_reason == "cancelled"

    with pytest.raises(RequestError) as type_error:
        await agent.prompt(
            [acp.resource_link_block("file", "file:///tmp/file")],
            created.session_id,
        )
    assert type_error.value.code == -32602


@pytest.mark.asyncio
async def test_event_mapper_deduplicates_cumulative_reasoning_and_artifacts() -> None:
    updates: list[Any] = []

    async def send(update: Any) -> None:
        updates.append(update)

    block = acp.resource_link_block("report.txt", "file:///tmp/report.txt", mime_type="text/plain")
    mapper = ACPEventMapper("session-1", send, artifact_resolver=lambda path: block if path == "/out" else None)
    await mapper.handle(
        SimpleNamespace(
            type="messages-tuple",
            data={"type": "ai", "id": "x", "content": "", "reasoning_content": "one"},
        )
    )
    await mapper.handle(
        SimpleNamespace(
            type="messages-tuple",
            data={"type": "ai", "id": "x", "content": "", "reasoning_content": "one two"},
        )
    )
    values = SimpleNamespace(type="values", data={"artifacts": ["/out", "/out"], "todos": []})
    await mapper.handle(values)
    await mapper.handle(values)

    thoughts = [update.content.text for update in updates if isinstance(update, schema.AgentThoughtChunk)]
    resources = [update for update in updates if isinstance(update, schema.AgentMessageChunk)]
    plans = [update for update in updates if isinstance(update, schema.AgentPlanUpdate)]
    assert thoughts == ["one", " two"]
    assert len(resources) == 1
    assert len(plans) == 1


@pytest.mark.asyncio
async def test_event_mapper_can_start_subagent_from_progress_or_terminal_event() -> None:
    updates: list[Any] = []

    async def send(update: Any) -> None:
        updates.append(update)

    mapper = ACPEventMapper("session-1", send)
    await mapper.handle_live(
        {"type": "token_chunk", "task_id": "late-1", "content": "progress"}
    )
    await mapper.handle_live(
        {"type": "task_completed", "task_id": "late-1", "result": "done"}
    )
    await mapper.close_open_tools(cancelled=False)
    await mapper.handle_live(
        {"type": "token_chunk", "task_id": "late-1", "content": "too late"}
    )

    assert sum(isinstance(update, schema.ToolCallStart) for update in updates) == 1
    progress = [update for update in updates if isinstance(update, schema.ToolCallProgress)]
    assert [update.status for update in progress] == ["in_progress", "completed"]


@pytest.mark.asyncio
async def test_session_store_persists_and_pages(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    first = LocalACPSessionStore(path)
    first.setup()
    defaults = {
        "model_name": None,
        "thinking_enabled": True,
        "subagent_enabled": False,
        "plan_mode": False,
        "max_concurrent_subagents": 2,
        "recursion_limit": 100,
        "agent_name": None,
    }
    one = await first.create(cwd=str(tmp_path), defaults=defaults)
    await first.create(cwd=str(tmp_path), defaults=defaults)
    await first.create(cwd=str(tmp_path / "different"), defaults=defaults)
    page, cursor = await first.list(cwd=None, cursor=None, limit=2)
    assert len(page) == 2 and cursor is not None
    second_page, next_cursor = await first.list(cwd=None, cursor=cursor, limit=2)
    assert len(second_page) == 1 and next_cursor is None
    first.close()

    reopened = LocalACPSessionStore(path)
    reopened.setup()
    loaded = await reopened.get(one.session_id)
    assert loaded is not None and loaded.cwd == str(tmp_path)
    reopened.close()
