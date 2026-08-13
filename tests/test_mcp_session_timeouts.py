from __future__ import annotations

import asyncio
import logging
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from deerflow.config.extensions_config import ExtensionsConfig, McpServerConfig
from deerflow.constants import DEFAULT_MCP_SESSION_INIT_TIMEOUT
from deerflow.mcp.session_pool import MCPSessionPool
from deerflow.mcp.tools import _make_session_pool_tool, get_mcp_tools


class _Args(BaseModel):
    query: str


def _tool(name: str) -> StructuredTool:
    async def call(query: str) -> str:
        return query

    return StructuredTool(
        name=name,
        description="Search",
        args_schema=_Args,
        coroutine=call,
    )


def test_session_init_timeout_defaults_and_explicit_opt_out() -> None:
    assert McpServerConfig().session_init_timeout == DEFAULT_MCP_SESSION_INIT_TIMEOUT
    assert McpServerConfig(session_init_timeout=None).session_init_timeout is None


@pytest.mark.asyncio
async def test_discovery_timeout_skips_only_hung_server() -> None:
    config = ExtensionsConfig.model_validate(
        {
            "mcpServers": {
                "slow": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["slow"],
                    "session_init_timeout": 0.02,
                },
                "fast": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["fast"],
                    "session_init_timeout": 1,
                },
            }
        }
    )
    connections = {
        "slow": {"transport": "stdio", "command": "npx", "args": ["slow"]},
        "fast": {"transport": "stdio", "command": "npx", "args": ["fast"]},
    }

    class FakeClient:
        def __init__(self, _connections, **kwargs) -> None:
            self.callbacks = None
            self.tool_interceptors = kwargs.get("tool_interceptors", [])

        async def get_tools(self, *, server_name=None):
            if server_name == "slow":
                await asyncio.sleep(60)
            return [_tool(f"{server_name}_search")]

    with (
        patch("deerflow.mcp.tools.build_servers_config", return_value=connections),
        patch("deerflow.mcp.tools.get_initial_oauth_headers", new_callable=AsyncMock, return_value={}),
        patch("deerflow.mcp.tools.build_oauth_tool_interceptor", return_value=None),
        patch("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient),
        patch("deerflow.mcp.tools._make_session_pool_tool", side_effect=lambda tool, *_args, **_kwargs: tool),
    ):
        tools = await asyncio.wait_for(get_mcp_tools(config), timeout=2)

    assert [tool.name for tool in tools] == ["fast_search"]


@pytest.mark.asyncio
async def test_persistent_session_timeout_is_logged_and_raised(caplog) -> None:
    pool = MagicMock()

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(60)

    pool.get_session = hang
    with (
        patch("deerflow.mcp.tools.get_session_pool", return_value=pool),
        caplog.at_level(logging.WARNING, logger="deerflow.mcp.tools"),
    ):
        wrapped = _make_session_pool_tool(
            _tool("github_search"),
            "github",
            {"transport": "stdio", "command": "npx", "args": ["github"]},
            tool_name_prefix=False,
            session_init_timeout=0.02,
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(wrapped.coroutine(query="repo"), timeout=1)

    assert any("github" in record.getMessage() and "timed out" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_cancelled_session_initialization_unwinds_transport_context() -> None:
    pool = MCPSessionPool()

    exited = threading.Event()

    class Session:
        async def initialize(self) -> None:
            await asyncio.sleep(60)

    class ContextManager:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            exited.set()

    with (
        patch("langchain_mcp_adapters.sessions.create_session", return_value=ContextManager()),
        pytest.raises(TimeoutError),
    ):
        await asyncio.wait_for(
            pool.get_session("slow", "thread-1", {"transport": "stdio"}),
            timeout=0.02,
        )

    assert exited.is_set()
    assert list(pool._entries) == []


@pytest.mark.asyncio
async def test_concurrent_first_use_creates_only_one_persistent_session() -> None:
    pool = MCPSessionPool()

    initialize_started = threading.Event()
    allow_initialize = threading.Event()
    entered = 0
    exited = 0

    class Session:
        async def initialize(self) -> None:
            initialize_started.set()
            await asyncio.to_thread(allow_initialize.wait)

    class ContextManager:
        async def __aenter__(self):
            nonlocal entered
            entered += 1
            return Session()

        async def __aexit__(self, *_args):
            nonlocal exited
            exited += 1

    with patch(
        "langchain_mcp_adapters.sessions.create_session",
        side_effect=lambda _connection: ContextManager(),
    ):
        first = asyncio.create_task(
            pool.get_session("server", "thread-1", {"transport": "stdio"})
        )
        await asyncio.to_thread(initialize_started.wait)
        second = asyncio.create_task(
            pool.get_session("server", "thread-1", {"transport": "stdio"})
        )
        await asyncio.sleep(0.03)

        assert entered == 1
        allow_initialize.set()
        first_session, second_session = await asyncio.gather(first, second)

    assert first_session is second_session
    assert entered == 1
    assert exited == 0
    assert pool._creation_guards == {}
    await pool.close_all()
    assert exited == 1


@pytest.mark.asyncio
async def test_cancelled_session_waiter_does_not_strand_creation_guard() -> None:
    pool = MCPSessionPool()

    initialize_started = threading.Event()
    allow_initialize = threading.Event()

    class Session:
        async def initialize(self) -> None:
            initialize_started.set()
            await asyncio.to_thread(allow_initialize.wait)

    class ContextManager:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return None

    with patch(
        "langchain_mcp_adapters.sessions.create_session",
        side_effect=lambda _connection: ContextManager(),
    ):
        creator = asyncio.create_task(
            pool.get_session("server", "thread-1", {"transport": "stdio"})
        )
        await asyncio.to_thread(initialize_started.wait)
        waiter = asyncio.create_task(
            pool.get_session("server", "thread-1", {"transport": "stdio"})
        )
        await asyncio.sleep(0.03)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        allow_initialize.set()
        created = await creator
        reused = await asyncio.wait_for(
            pool.get_session("server", "thread-1", {"transport": "stdio"}),
            timeout=1,
        )

    assert reused is created
    assert pool._creation_guards == {}
    await pool.close_all()


def test_sync_calls_reuse_one_actor_owned_persistent_session() -> None:
    """Fresh asyncio.run loops must not replace stateful MCP sessions."""
    pool = MCPSessionPool()
    entered = 0
    exited = 0
    owner_thread_ids: set[int] = set()

    class Session:
        async def initialize(self) -> None:
            owner_thread_ids.add(threading.get_ident())

        async def call_tool(self, name: str, arguments: dict) -> dict:
            owner_thread_ids.add(threading.get_ident())
            return {"name": name, "arguments": arguments}

    class ContextManager:
        async def __aenter__(self):
            nonlocal entered
            entered += 1
            owner_thread_ids.add(threading.get_ident())
            return Session()

        async def __aexit__(self, *_args):
            nonlocal exited
            exited += 1
            owner_thread_ids.add(threading.get_ident())

    async def invoke(value: int):
        session = await pool.get_session("server", "thread-1", {"transport": "stdio"})
        result = await session.call_tool("remember", {"value": value})
        return session, result

    with patch(
        "langchain_mcp_adapters.sessions.create_session",
        side_effect=lambda _connection: ContextManager(),
    ):
        first, first_result = asyncio.run(invoke(1))
        second, second_result = asyncio.run(invoke(2))
        pool.close_all_sync()

    assert first is second
    assert first_result["arguments"] == {"value": 1}
    assert second_result["arguments"] == {"value": 2}
    assert entered == 1
    assert exited == 1
    assert len(owner_thread_ids) == 1
