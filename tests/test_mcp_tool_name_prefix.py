from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from deerflow.config.extensions_config import ExtensionsConfig, McpServerConfig
from deerflow.mcp.client import build_server_params
from deerflow.mcp.tools import _make_session_pool_tool, get_mcp_tools


class _Args(BaseModel):
    query: str


def _tool(name: str) -> StructuredTool:
    async def call(query: str) -> str:
        return query

    return StructuredTool(name=name, description="Search", args_schema=_Args, coroutine=call)


def test_mcp_tool_name_prefix_defaults_true_and_can_be_disabled() -> None:
    assert McpServerConfig().tool_name_prefix is True
    assert McpServerConfig(tool_name_prefix=False).model_dump()["tool_name_prefix"] is False


@pytest.mark.parametrize("transport", ["streamable_http", "websocket"])
def test_mcp_client_builds_every_api_accepted_remote_transport(transport: str) -> None:
    config = McpServerConfig(type=transport, url="wss://example.test/mcp" if transport == "websocket" else "https://example.test/mcp")

    params = build_server_params("remote", config)

    assert params["transport"] == transport
    assert params["url"] == config.url


@pytest.mark.asyncio
async def test_mcp_discovery_keeps_per_server_source_with_overlapping_names() -> None:
    config = ExtensionsConfig.model_validate(
        {
            "mcpServers": {
                "web": {"type": "stdio", "command": "npx", "args": ["web"]},
                "web_scraper": {"type": "stdio", "command": "npx", "args": ["scraper"]},
            }
        }
    )
    connections = {
        "web": {"transport": "stdio", "command": "npx", "args": ["web"]},
        "web_scraper": {"transport": "stdio", "command": "npx", "args": ["scraper"]},
    }

    class FakeClient:
        def __init__(self, _connections, **kwargs) -> None:
            self.callbacks = None
            self.tool_interceptors = kwargs.get("tool_interceptors", [])

        async def get_tools(self, *, server_name=None):
            return [_tool(f"{server_name}_search")]

    routed: list[tuple[str, str]] = []

    def wrap(tool, server_name, *_args, **_kwargs):
        routed.append((tool.name, server_name))
        return tool

    with (
        patch("deerflow.mcp.tools.ExtensionsConfig.from_file", return_value=config),
        patch("deerflow.mcp.tools.build_servers_config", return_value=connections),
        patch("deerflow.mcp.tools.get_initial_oauth_headers", new_callable=AsyncMock, return_value={}),
        patch("deerflow.mcp.tools.build_oauth_tool_interceptor", return_value=None),
        patch("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient),
        patch("deerflow.mcp.tools._make_session_pool_tool", side_effect=wrap),
    ):
        tools = await get_mcp_tools()

    assert {tool.name for tool in tools} == {"web_search", "web_scraper_search"}
    assert routed == [("web_search", "web"), ("web_scraper_search", "web_scraper")]


@pytest.mark.asyncio
async def test_per_server_prefix_opt_out_and_collision_detection() -> None:
    config = ExtensionsConfig.model_validate(
        {
            "mcpServers": {
                "one": {"type": "stdio", "command": "npx", "args": ["one"], "tool_name_prefix": False},
                "two": {"type": "stdio", "command": "npx", "args": ["two"], "tool_name_prefix": False},
            }
        }
    )
    connections = {
        "one": {"transport": "stdio", "command": "npx", "args": ["one"]},
        "two": {"transport": "stdio", "command": "npx", "args": ["two"]},
    }

    class FakeClient:
        def __init__(self, _connections, **kwargs) -> None:
            self.callbacks = None
            self.tool_interceptors = kwargs.get("tool_interceptors", [])

        async def get_tools(self, *, server_name=None):
            raise AssertionError("Prefix-disabled discovery must use load_mcp_tools directly")

    async def load_one(_session, **_kwargs):
        return [_tool("search")]

    with (
        patch("deerflow.mcp.tools.ExtensionsConfig.from_file", return_value=config),
        patch("deerflow.mcp.tools.build_servers_config", return_value=connections),
        patch("deerflow.mcp.tools.get_initial_oauth_headers", new_callable=AsyncMock, return_value={}),
        patch("deerflow.mcp.tools.build_oauth_tool_interceptor", return_value=None),
        patch("langchain_mcp_adapters.client.MultiServerMCPClient", FakeClient),
        patch("langchain_mcp_adapters.tools.load_mcp_tools", side_effect=load_one) as loader,
        patch("deerflow.mcp.tools._make_session_pool_tool") as wrapper,
    ):
        tools = await get_mcp_tools()

    assert tools == []
    assert loader.await_count == 2
    wrapper.assert_not_called()


@pytest.mark.asyncio
async def test_unprefixed_tool_keeps_server_like_original_name() -> None:
    original_tool = _tool("github_search")
    session = AsyncMock()
    session.call_tool = AsyncMock(return_value=MagicMock(content=[], isError=False, structuredContent=None))
    pool = MagicMock()
    pool.get_session = AsyncMock(return_value=session)

    with patch("deerflow.mcp.tools.get_session_pool", return_value=pool):
        wrapped = _make_session_pool_tool(
            original_tool,
            "github",
            {"transport": "stdio", "command": "npx", "args": ["github"]},
            tool_name_prefix=False,
        )
        await wrapped.coroutine(query="repositories")

    session.call_tool.assert_awaited_once_with("github_search", {"query": "repositories"})
    assert wrapped.metadata["mcp_server"] == "github"
    assert wrapped.metadata["mcp_original_tool_name"] == "github_search"
