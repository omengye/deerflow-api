"""Tests for the invoke_acp_agent tool's timeout handling.

These tests stub out ``acp.spawn_agent_process`` so no real ACP subprocess is
spawned. The ACP wire types (``PROTOCOL_VERSION``, ``Client``, ``text_block``,
schema classes) are used as-is from the real ``acp`` package since they carry
no I/O.
"""

import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from deerflow.config.acp_config import ACPAgentConfig
from deerflow.tools.builtins.acp_artifact_downloader import DownloadedACPArtifact
from deerflow.tools.builtins.invoke_acp_agent_tool import build_invoke_acp_agent_tool


def _agent_message(text: str):
    import acp
    from acp import schema

    return schema.AgentMessageChunk(
        session_update="agent_message_chunk",
        content=acp.text_block(text),
        message_id="11111111-1111-1111-1111-111111111111",
    )


def _agent_thought(text: str):
    import acp
    from acp import schema

    return schema.AgentThoughtChunk(
        session_update="agent_thought_chunk",
        content=acp.text_block(text),
        message_id="22222222-2222-2222-2222-222222222222",
    )


def _agent_resource(name: str, uri: str):
    import acp
    from acp import schema

    return schema.AgentMessageChunk(
        session_update="agent_message_chunk",
        content=acp.resource_link_block(name, uri, size=7),
        message_id="33333333-3333-3333-3333-333333333333",
    )


class _FakePaths:
    """Stand-in for deerflow.config.paths.Paths that skips real base_dir resolution."""

    def __init__(self, acp_workspace_dir) -> None:
        self._acp_workspace_dir = acp_workspace_dir

    def acp_workspace_dir(self, thread_id: str):
        return self._acp_workspace_dir


class _FakeConnection:
    """Stand-in for acp.ClientSideConnection used by tests below."""

    def __init__(self, client, *, hang: bool, response_text: str) -> None:
        self._client = client
        self._hang = hang
        self._response_text = response_text
        self.prompt_calls: list[dict] = []

    async def initialize(self, **kwargs) -> None:
        return None

    async def new_session(self, **kwargs):
        return SimpleNamespace(session_id="sess-1")

    async def prompt(self, **kwargs) -> None:
        self.prompt_calls.append(kwargs)
        if self._hang:
            # Simulate an ACP agent subprocess that never responds.
            await asyncio.Event().wait()
            return

        await self._client.session_update(
            kwargs["session_id"],
            _agent_message(self._response_text),
        )


def _fake_spawn_agent_process(*, hang: bool, response_text: str = "hello from agent"):
    @asynccontextmanager
    async def _spawn(client, command, *args, env=None, cwd=None):
        conn = _FakeConnection(client, hang=hang, response_text=response_text)
        proc = SimpleNamespace(pid=1234)
        yield conn, proc

    return _spawn


def _agent_config(**overrides) -> ACPAgentConfig:
    fields = {
        "command": "fake-acp-agent",
        "args": [],
        "description": "Fake agent for tests",
    }
    fields.update(overrides)
    return ACPAgentConfig(**fields)


@pytest.fixture(autouse=True)
def _isolated_acp_workspace(tmp_path, monkeypatch):
    acp_dir = tmp_path / "threads" / "thread-1" / "acp-workspace"
    acp_dir.mkdir(parents=True)
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: _FakePaths(acp_dir))


async def _invoke(
    agent_config: ACPAgentConfig,
    *,
    prompt: str = "do the task",
    live_event_callback=None,
) -> str:
    tool = build_invoke_acp_agent_tool({"fake_agent": agent_config})
    config = {"configurable": {"thread_id": "thread-1"}}
    if live_event_callback is not None:
        config["metadata"] = {"live_event_callback": live_event_callback}
    return await tool.coroutine(
        agent="fake_agent",
        prompt=prompt,
        config=config,
    )


def test_acp_agent_config_defaults_timeout_to_600_seconds() -> None:
    assert _agent_config().timeout_seconds == 600


def test_acp_agent_config_allows_disabling_timeout() -> None:
    assert _agent_config(timeout_seconds=None).timeout_seconds is None


def test_acp_agent_config_disables_insecure_artifact_http_by_default() -> None:
    assert _agent_config().artifact_allow_insecure_http is False


async def test_invoke_acp_agent_returns_timeout_error_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("acp.spawn_agent_process", _fake_spawn_agent_process(hang=True))
    agent_config = _agent_config(timeout_seconds=0.1)
    live_events: list[dict] = []

    async def live_event_callback(event: dict) -> None:
        live_events.append(event)

    # Bound the test itself so a regression (missing timeout) fails fast
    # instead of hanging the suite.
    result = await asyncio.wait_for(
        _invoke(agent_config, live_event_callback=live_event_callback),
        timeout=5,
    )

    assert "fake_agent" in result
    assert "timed out after 0.1 seconds" in result
    assert "timeout_seconds" in result
    assert [event["type"] for event in live_events] == [
        "subagent_started",
        "task_timed_out",
    ]


async def test_invoke_acp_agent_returns_agent_response_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "acp.spawn_agent_process",
        _fake_spawn_agent_process(hang=False, response_text="42"),
    )
    agent_config = _agent_config(timeout_seconds=5)

    result = await _invoke(agent_config)

    assert result == "42"


async def test_invoke_acp_agent_offloads_blocking_setup_helpers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from deerflow.tools.builtins import invoke_acp_agent_tool as module

    event_loop_thread = threading.get_ident()
    helper_threads: dict[str, int] = {}

    def work_dir(_thread_id):
        helper_threads["work_dir"] = threading.get_ident()
        tmp_path.mkdir(parents=True, exist_ok=True)
        return str(tmp_path)

    def mcp_servers():
        helper_threads["mcp_servers"] = threading.get_ident()
        return []

    monkeypatch.setattr(module, "_get_work_dir", work_dir)
    monkeypatch.setattr(module, "_build_acp_mcp_servers", mcp_servers)
    monkeypatch.setattr(
        "acp.spawn_agent_process",
        _fake_spawn_agent_process(hang=False, response_text="ok"),
    )

    result = await _invoke(_agent_config(timeout_seconds=5))

    assert result == "ok"
    assert helper_threads.keys() == {"work_dir", "mcp_servers"}
    assert all(thread_id != event_loop_thread for thread_id in helper_threads.values())


async def test_invoke_acp_agent_offloads_executable_lookup_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deerflow.tools.builtins import invoke_acp_agent_tool as module

    event_loop_thread = threading.get_ident()
    formatting_thread: list[int] = []

    @asynccontextmanager
    async def missing_process(*_args, **_kwargs):
        raise FileNotFoundError("missing")
        yield  # pragma: no cover

    def format_error(_agent, _cmd, _error):
        formatting_thread.append(threading.get_ident())
        return "formatted failure"

    monkeypatch.setattr(module, "_build_acp_mcp_servers", list)
    monkeypatch.setattr(module, "_format_invocation_error", format_error)
    monkeypatch.setattr("acp.spawn_agent_process", missing_process)

    result = await _invoke(_agent_config(timeout_seconds=5))

    assert result == "formatted failure"
    assert len(formatting_thread) == 1
    assert formatting_thread[0] != event_loop_thread


async def test_invoke_acp_agent_forwards_live_chunks_before_prompt_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_events: list[dict] = []
    first_live_text = asyncio.Event()
    release_prompt = asyncio.Event()

    async def live_event_callback(event: dict) -> None:
        live_events.append(event)
        if event.get("type") == "token_chunk":
            first_live_text.set()

    class _StreamingConnection(_FakeConnection):
        async def prompt(self, **kwargs) -> None:
            self.prompt_calls.append(kwargs)
            await self._client.session_update(
                kwargs["session_id"],
                _agent_thought("thinking"),
            )
            await self._client.session_update(
                kwargs["session_id"],
                _agent_message("hello"),
            )
            await release_prompt.wait()
            await self._client.session_update(
                kwargs["session_id"],
                _agent_message(" world"),
            )

    @asynccontextmanager
    async def spawn(client, command, *args, env=None, cwd=None):
        yield (
            _StreamingConnection(client, hang=False, response_text=""),
            SimpleNamespace(pid=1),
        )

    monkeypatch.setattr("acp.spawn_agent_process", spawn)
    invocation = asyncio.create_task(
        _invoke(
            _agent_config(timeout_seconds=5),
            live_event_callback=live_event_callback,
        )
    )

    await asyncio.wait_for(first_live_text.wait(), timeout=1)
    assert not invocation.done()
    assert live_events[0]["type"] == "subagent_started"

    release_prompt.set()
    result = await asyncio.wait_for(invocation, timeout=5)

    assert result == "thinkinghello world"
    assert (
        "".join(
            event["content"] for event in live_events if event["type"] == "token_chunk"
        )
        == "hello world"
    )
    assert (
        "".join(
            event["thinking"]
            for event in live_events
            if event["type"] == "thinking_chunk"
        )
        == "thinking"
    )
    assert live_events[-1]["type"] == "task_completed"
    assert len({event["task_id"] for event in live_events}) == 1


async def test_invoke_acp_agent_coalesces_burst_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_events: list[dict] = []

    async def live_event_callback(event: dict) -> None:
        live_events.append(event)

    class _BurstConnection(_FakeConnection):
        async def prompt(self, **kwargs) -> None:
            for text in ("one", " ", "two"):
                await self._client.session_update(
                    kwargs["session_id"],
                    _agent_message(text),
                )

    @asynccontextmanager
    async def spawn(client, command, *args, env=None, cwd=None):
        yield (
            _BurstConnection(client, hang=False, response_text=""),
            SimpleNamespace(pid=1),
        )

    monkeypatch.setattr("acp.spawn_agent_process", spawn)
    result = await _invoke(
        _agent_config(timeout_seconds=5),
        live_event_callback=live_event_callback,
    )

    assert result == "one two"
    token_events = [event for event in live_events if event["type"] == "token_chunk"]
    assert [event["content"] for event in token_events] == ["one two"]


async def test_invoke_acp_agent_no_timeout_configured_still_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "acp.spawn_agent_process",
        _fake_spawn_agent_process(hang=False, response_text="ok"),
    )
    agent_config = _agent_config(timeout_seconds=None)

    result = await asyncio.wait_for(_invoke(agent_config), timeout=5)

    assert result == "ok"


async def test_invoke_acp_agent_serializes_calls_for_one_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    maximum = 0

    class _SlowConnection(_FakeConnection):
        async def prompt(self, **kwargs) -> None:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.05)
                await super().prompt(**kwargs)
            finally:
                active -= 1

    @asynccontextmanager
    async def spawn(client, command, *args, env=None, cwd=None):
        yield (
            _SlowConnection(client, hang=False, response_text="ok"),
            SimpleNamespace(pid=1),
        )

    monkeypatch.setattr("acp.spawn_agent_process", spawn)
    tool = build_invoke_acp_agent_tool({"fake_agent": _agent_config(timeout_seconds=5)})
    results = await asyncio.gather(
        tool.coroutine(
            agent="fake_agent",
            prompt="one",
            config={"configurable": {"thread_id": "thread-1"}},
        ),
        tool.coroutine(
            agent="fake_agent",
            prompt="two",
            config={"configurable": {"thread_id": "thread-1"}},
        ),
    )

    assert results == ["ok", "ok"]
    assert maximum == 1


async def test_invoke_acp_agent_reports_downloaded_resource_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader_options: dict[str, object] = {}

    class _FakeDownloader:
        def __init__(self, *args, **kwargs) -> None:
            downloader_options.update(kwargs)

        async def download(self, resource):
            return DownloadedACPArtifact(
                name=resource.name,
                virtual_path="/mnt/acp-workspace/invocation/report.txt",
                size=7,
                sha256="a" * 64,
                mime_type="text/plain",
            )

    class _ResourceConnection(_FakeConnection):
        async def prompt(self, **kwargs) -> None:
            await self._client.session_update(
                kwargs["session_id"],
                _agent_resource(
                    "report.txt",
                    "https://rustfs.example.test/report.txt",
                ),
            )

    @asynccontextmanager
    async def spawn(client, command, *args, env=None, cwd=None):
        yield (
            _ResourceConnection(client, hang=False, response_text=""),
            SimpleNamespace(pid=1),
        )

    monkeypatch.setattr("acp.spawn_agent_process", spawn)
    monkeypatch.setattr(
        "deerflow.tools.builtins.invoke_acp_agent_tool.ACPArtifactDownloader",
        _FakeDownloader,
    )
    result = await _invoke(
        _agent_config(timeout_seconds=5, artifact_allow_insecure_http=True)
    )

    assert "/mnt/acp-workspace/invocation/report.txt" in result
    assert "sha256=" + "a" * 64 in result
    assert downloader_options["allow_insecure_http"] is True


async def test_artifact_download_does_not_block_live_text_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    download_started = asyncio.Event()
    release_download = asyncio.Event()
    live_text_received = asyncio.Event()

    class _SlowDownloader:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def download(self, resource):
            download_started.set()
            await release_download.wait()
            return DownloadedACPArtifact(
                name=resource.name,
                virtual_path="/mnt/acp-workspace/invocation/report.txt",
                size=7,
                sha256="b" * 64,
                mime_type="text/plain",
            )

    class _ResourceThenTextConnection(_FakeConnection):
        async def prompt(self, **kwargs) -> None:
            await self._client.session_update(
                kwargs["session_id"],
                _agent_resource(
                    "report.txt",
                    "https://rustfs.example.test/report.txt",
                ),
            )
            await self._client.session_update(
                kwargs["session_id"],
                _agent_message("text after artifact"),
            )

    @asynccontextmanager
    async def spawn(client, command, *args, env=None, cwd=None):
        yield (
            _ResourceThenTextConnection(client, hang=False, response_text=""),
            SimpleNamespace(pid=1),
        )

    async def live_event_callback(event: dict) -> None:
        if event.get("type") == "token_chunk":
            live_text_received.set()

    monkeypatch.setattr("acp.spawn_agent_process", spawn)
    monkeypatch.setattr(
        "deerflow.tools.builtins.invoke_acp_agent_tool.ACPArtifactDownloader",
        _SlowDownloader,
    )
    invocation = asyncio.create_task(
        _invoke(
            _agent_config(timeout_seconds=5, artifact_allow_insecure_http=True),
            live_event_callback=live_event_callback,
        )
    )

    await asyncio.wait_for(download_started.wait(), timeout=1)
    await asyncio.wait_for(live_text_received.wait(), timeout=1)
    assert not invocation.done()

    release_download.set()
    result = await asyncio.wait_for(invocation, timeout=5)
    assert result.startswith("text after artifact")
    assert "/mnt/acp-workspace/invocation/report.txt" in result
