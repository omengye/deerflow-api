"""Tests for the invoke_acp_agent tool's timeout handling.

These tests stub out ``acp.spawn_agent_process`` so no real ACP subprocess is
spawned. The ACP wire types (``PROTOCOL_VERSION``, ``Client``, ``text_block``,
schema classes) are used as-is from the real ``acp`` package since they carry
no I/O.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from deerflow.config.acp_config import ACPAgentConfig
from deerflow.tools.builtins.acp_artifact_downloader import DownloadedACPArtifact
from deerflow.tools.builtins.invoke_acp_agent_tool import build_invoke_acp_agent_tool


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

        from acp.schema import TextContentBlock

        update = SimpleNamespace(content=TextContentBlock(type="text", text=self._response_text))
        await self._client.session_update(kwargs["session_id"], update)


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


async def _invoke(agent_config: ACPAgentConfig, *, prompt: str = "do the task") -> str:
    tool = build_invoke_acp_agent_tool({"fake_agent": agent_config})
    return await tool.coroutine(
        agent="fake_agent",
        prompt=prompt,
        config={"configurable": {"thread_id": "thread-1"}},
    )


def test_acp_agent_config_defaults_timeout_to_600_seconds() -> None:
    assert _agent_config().timeout_seconds == 600


def test_acp_agent_config_allows_disabling_timeout() -> None:
    assert _agent_config(timeout_seconds=None).timeout_seconds is None


def test_acp_agent_config_disables_insecure_artifact_http_by_default() -> None:
    assert _agent_config().artifact_allow_insecure_http is False


async def test_invoke_acp_agent_returns_timeout_error_instead_of_hanging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("acp.spawn_agent_process", _fake_spawn_agent_process(hang=True))
    agent_config = _agent_config(timeout_seconds=0.1)

    # Bound the test itself so a regression (missing timeout) fails fast
    # instead of hanging the suite.
    result = await asyncio.wait_for(_invoke(agent_config), timeout=5)

    assert "fake_agent" in result
    assert "timed out after 0.1 seconds" in result
    assert "timeout_seconds" in result


async def test_invoke_acp_agent_returns_agent_response_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("acp.spawn_agent_process", _fake_spawn_agent_process(hang=False, response_text="42"))
    agent_config = _agent_config(timeout_seconds=5)

    result = await _invoke(agent_config)

    assert result == "42"


async def test_invoke_acp_agent_no_timeout_configured_still_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("acp.spawn_agent_process", _fake_spawn_agent_process(hang=False, response_text="ok"))
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
        yield _SlowConnection(client, hang=False, response_text="ok"), SimpleNamespace(pid=1)

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
            from acp import resource_link_block

            update = SimpleNamespace(
                content=resource_link_block(
                    "report.txt",
                    "https://rustfs.example.test/report.txt",
                    size=7,
                )
            )
            await self._client.session_update(kwargs["session_id"], update)

    @asynccontextmanager
    async def spawn(client, command, *args, env=None, cwd=None):
        yield _ResourceConnection(client, hang=False, response_text=""), SimpleNamespace(pid=1)

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
