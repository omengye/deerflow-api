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
