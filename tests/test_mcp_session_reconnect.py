from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from langchain_core.tools import StructuredTool
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED, CallToolResult, ErrorData
from pydantic import BaseModel, Field

from deerflow.mcp.session_pool import MCPSessionPool, OwnedMCPSession
from deerflow.mcp.tools import _make_session_pool_tool


class _Args(BaseModel):
    value: int = Field(default=1)


def _wrapped_tool(*, pool: MagicMock, outcome, interceptors=None):
    original = StructuredTool(
        name="srv_act",
        description="test tool",
        args_schema=_Args,
        coroutine=AsyncMock(),
        response_format="content_and_artifact",
    )
    session = AsyncMock(spec=OwnedMCPSession)
    if isinstance(outcome, BaseException):
        session.call_tool.side_effect = outcome
    else:
        session.call_tool.return_value = outcome
    pool.get_session = AsyncMock(return_value=session)
    pool.close_session_if_current = AsyncMock(return_value=True)

    with patch("deerflow.mcp.tools.get_session_pool", return_value=pool):
        wrapped = _make_session_pool_tool(
            original,
            "srv",
            {"transport": "stdio", "command": "test"},
            tool_interceptors=interceptors,
        )
    return wrapped, session


@pytest.mark.parametrize(
    "transport_error",
    [
        anyio.ClosedResourceError(),
        anyio.BrokenResourceError(),
        anyio.EndOfStream(),
        McpError(ErrorData(code=CONNECTION_CLOSED, message="Connection closed")),
    ],
)
async def test_dead_transport_evicts_exact_pooled_session(transport_error) -> None:
    pool = MagicMock(spec=MCPSessionPool)
    wrapped, session = _wrapped_tool(pool=pool, outcome=transport_error)

    with pytest.raises(type(transport_error)):
        await wrapped.coroutine(value=1)

    pool.close_session_if_current.assert_awaited_once_with(
        "srv",
        "default",
        session,
    )


async def test_protocol_error_does_not_evict_healthy_transport() -> None:
    error = McpError(ErrorData(code=408, message="request timed out"))
    pool = MagicMock(spec=MCPSessionPool)
    wrapped, _session = _wrapped_tool(pool=pool, outcome=error)

    with pytest.raises(McpError, match="request timed out"):
        await wrapped.coroutine(value=1)

    pool.close_session_if_current.assert_not_awaited()


async def test_interceptor_error_does_not_evict_transport() -> None:
    async def failing_interceptor(_request, _handler):
        raise RuntimeError("interceptor failed")

    pool = MagicMock(spec=MCPSessionPool)
    wrapped, session = _wrapped_tool(
        pool=pool,
        outcome=CallToolResult(content=[], isError=False),
        interceptors=[failing_interceptor],
    )

    with pytest.raises(RuntimeError, match="interceptor failed"):
        await wrapped.coroutine(value=1)

    session.call_tool.assert_not_awaited()
    pool.close_session_if_current.assert_not_awaited()


async def test_cleanup_failure_preserves_original_disconnect() -> None:
    error = anyio.ClosedResourceError()
    pool = MagicMock(spec=MCPSessionPool)
    wrapped, _session = _wrapped_tool(pool=pool, outcome=error)
    pool.close_session_if_current.side_effect = RuntimeError("cleanup failed")

    with pytest.raises(anyio.ClosedResourceError) as exc_info:
        await wrapped.coroutine(value=1)

    assert exc_info.value is error


async def test_pool_does_not_close_replacement_for_late_failure() -> None:
    pool = MCPSessionPool()
    old_session = AsyncMock(spec=OwnedMCPSession)
    replacement = AsyncMock(spec=OwnedMCPSession)
    pool._entries[("srv", "thread")] = replacement

    assert not await pool.close_session_if_current("srv", "thread", old_session)
    replacement.aclose.assert_not_awaited()
    assert pool._entries[("srv", "thread")] is replacement

    assert await pool.close_session_if_current("srv", "thread", replacement)
    replacement.aclose.assert_awaited_once()
    assert ("srv", "thread") not in pool._entries
    await pool.close_all()


async def test_real_stdio_disconnect_allows_later_reconnect(tmp_path) -> None:
    """A crashed stdio process must not poison later calls in the same thread."""
    server = """
import os
import sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP

marker = Path(sys.argv[1])
mcp = FastMCP("crash-once")

@mcp.tool()
def crash_once() -> str:
    if not marker.exists():
        marker.write_text("crashed", encoding="utf-8")
        os._exit(17)
    return "recovered"

mcp.run(transport="stdio")
"""

    class _NoArgs(BaseModel):
        pass

    original = StructuredTool(
        name="crash_crash_once",
        description="crash once",
        args_schema=_NoArgs,
        coroutine=AsyncMock(),
        response_format="content_and_artifact",
    )
    marker = tmp_path / "crashed"
    connection = {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-c", server, str(marker)],
    }
    runtime = SimpleNamespace(context={"thread_id": "thread"})
    pool = MCPSessionPool()

    with patch("deerflow.mcp.tools.get_session_pool", return_value=pool):
        wrapped = _make_session_pool_tool(original, "crash", connection)

    try:
        with pytest.raises(
            (
                McpError,
                anyio.ClosedResourceError,
                anyio.BrokenResourceError,
                anyio.EndOfStream,
            )
        ):
            await wrapped.coroutine(runtime=runtime)

        assert ("crash", "thread") not in pool._entries
        content, _artifact = await wrapped.coroutine(runtime=runtime)
        assert content[0]["text"] == "recovered"
    finally:
        await pool.close_all()
