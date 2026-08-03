"""Load MCP tools using langchain-mcp-adapters with persistent sessions."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.config import get_config
from langgraph.runtime import Runtime

from deerflow.config.extensions_config import ExtensionsConfig
from deerflow.mcp.client import build_servers_config
from deerflow.mcp.oauth import build_oauth_tool_interceptor, get_initial_oauth_headers
from deerflow.mcp.session_pool import get_session_pool
from deerflow.reflection import resolve_variable
from deerflow.tools.sync import make_sync_tool_wrapper

logger = logging.getLogger(__name__)

# Backward-compatible alias for older internal callers.
_make_sync_tool_wrapper = make_sync_tool_wrapper


def _extract_thread_id(runtime: Runtime | None) -> str:
    """Extract thread_id from the injected tool runtime or LangGraph config."""
    if runtime is not None:
        tid = runtime.context.get("thread_id") if runtime.context else None
        if tid is not None:
            return str(tid)

    try:
        tid = get_config().get("configurable", {}).get("thread_id")
        return str(tid) if tid is not None else "default"
    except RuntimeError:
        return "default"


def _convert_call_tool_result(call_tool_result: Any) -> Any:
    """Convert an MCP CallToolResult to the LangChain ``content_and_artifact`` format.

    Mirrors the behavior of ``langchain_mcp_adapters.tools._convert_call_tool_result``
    without depending on the private symbol. Pass-through is applied for
    ``ToolMessage`` (interceptor short-circuit) and ``langgraph.types.Command``.
    """
    from langchain_core.messages import ToolMessage
    from langchain_core.messages.content import create_file_block, create_image_block, create_text_block
    from langchain_core.tools import ToolException
    from mcp.types import EmbeddedResource, ImageContent, ResourceLink, TextContent, TextResourceContents

    if isinstance(call_tool_result, ToolMessage):
        return call_tool_result, None

    try:
        from langgraph.types import Command

        if isinstance(call_tool_result, Command):
            return call_tool_result, None
    except ImportError:
        pass

    lc_content = []
    for item in call_tool_result.content:
        if isinstance(item, TextContent):
            lc_content.append(create_text_block(text=item.text))
        elif isinstance(item, ImageContent):
            lc_content.append(create_image_block(base64=item.data, mime_type=item.mimeType))
        elif isinstance(item, ResourceLink):
            mime = item.mimeType or None
            if mime and mime.startswith("image/"):
                lc_content.append(create_image_block(url=str(item.uri), mime_type=mime))
            else:
                lc_content.append(create_file_block(url=str(item.uri), mime_type=mime))
        elif isinstance(item, EmbeddedResource):
            from mcp.types import BlobResourceContents

            res = item.resource
            if isinstance(res, TextResourceContents):
                lc_content.append(create_text_block(text=res.text))
            elif isinstance(res, BlobResourceContents):
                mime = res.mimeType or None
                if mime and mime.startswith("image/"):
                    lc_content.append(create_image_block(base64=res.blob, mime_type=mime))
                else:
                    lc_content.append(create_file_block(base64=res.blob, mime_type=mime))
            else:
                lc_content.append(create_text_block(text=str(res)))
        else:
            lc_content.append(create_text_block(text=str(item)))

    if call_tool_result.isError:
        error_parts = [item["text"] for item in lc_content if isinstance(item, dict) and item.get("type") == "text"]
        raise ToolException("\n".join(error_parts) if error_parts else str(lc_content))

    artifact = None
    if call_tool_result.structuredContent is not None:
        artifact = {"structured_content": call_tool_result.structuredContent}

    return lc_content, artifact


def _make_session_pool_tool(
    tool: BaseTool,
    server_name: str,
    connection: dict[str, Any],
    tool_interceptors: list[Any] | None = None,
    tool_name_prefix: bool = True,
) -> BaseTool:
    """Wrap an MCP tool so it reuses a persistent session from the pool.

    Replaces the per-call session creation with pool-managed sessions scoped
    by ``(server_name, thread_id)``. This ensures stateful MCP servers (e.g.
    Playwright) keep their state across tool calls within the same thread.

    The configured ``tool_interceptors`` (OAuth, custom) are preserved and
    applied on every call before invoking the pooled session.
    """
    # Strip only a prefix added by the adapter. An unprefixed server may expose
    # a tool whose original name itself begins with ``<server_name>_``.
    original_name = tool.name
    prefix = f"{server_name}_"
    if tool_name_prefix and original_name.startswith(prefix):
        original_name = original_name[len(prefix) :]

    pool = get_session_pool()

    async def call_with_persistent_session(
        runtime: Runtime | None = None,
        **arguments: Any,
    ) -> Any:
        thread_id = _extract_thread_id(runtime)
        session = await pool.get_session(server_name, thread_id, connection)

        if tool_interceptors:
            from langchain_mcp_adapters.interceptors import MCPToolCallRequest

            async def base_handler(request: MCPToolCallRequest) -> Any:
                return await session.call_tool(request.name, request.args)

            handler = base_handler
            for interceptor in reversed(tool_interceptors):
                outer = handler

                async def wrapped(req: Any, _i: Any = interceptor, _h: Any = outer) -> Any:
                    return await _i(req, _h)

                handler = wrapped

            request = MCPToolCallRequest(
                name=original_name,
                args=arguments,
                server_name=server_name,
                runtime=runtime,
            )
            call_tool_result = await handler(request)
        else:
            call_tool_result = await session.call_tool(original_name, arguments)

        return _convert_call_tool_result(call_tool_result)

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=call_with_persistent_session,
        response_format="content_and_artifact",
        metadata={
            **(tool.metadata or {}),
            "mcp_server": server_name,
            "mcp_original_tool_name": original_name,
        },
    )


async def get_mcp_tools() -> list[BaseTool]:
    """Get all tools from enabled MCP servers.

    Tools are wrapped with persistent-session logic so that consecutive
    calls within the same thread reuse the same MCP session.

    Returns:
        List of LangChain tools from all enabled MCP servers.
    """
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain_mcp_adapters.tools import load_mcp_tools
    except ImportError:
        logger.warning("langchain-mcp-adapters not installed. Install it to enable MCP tools: pip install langchain-mcp-adapters")
        return []

    # NOTE: We use ExtensionsConfig.from_file() instead of get_extensions_config()
    # to always read the latest configuration from disk. This ensures that changes
    # made through the Gateway API (which runs in a separate process) are immediately
    # reflected when initializing MCP tools.
    extensions_config = ExtensionsConfig.from_file()
    servers_config = build_servers_config(extensions_config)

    if not servers_config:
        logger.info("No enabled MCP servers configured")
        return []

    try:
        logger.info(f"Initializing MCP client with {len(servers_config)} server(s)")

        # Inject initial OAuth headers for server connections (tool discovery/session init)
        initial_oauth_headers = await get_initial_oauth_headers(extensions_config)
        for server_name, auth_header in initial_oauth_headers.items():
            if server_name not in servers_config:
                continue
            if servers_config[server_name].get("transport") in ("sse", "http", "streamable_http"):
                existing_headers = dict(servers_config[server_name].get("headers", {}))
                existing_headers["Authorization"] = auth_header
                servers_config[server_name]["headers"] = existing_headers

        tool_interceptors: list[Any] = []
        oauth_interceptor = build_oauth_tool_interceptor(extensions_config)
        if oauth_interceptor is not None:
            tool_interceptors.append(oauth_interceptor)

        # Load custom interceptors declared in extensions_config.json
        # Format: "mcpInterceptors": ["pkg.module:builder_func", ...]
        raw_interceptor_paths = (extensions_config.model_extra or {}).get("mcpInterceptors")
        if isinstance(raw_interceptor_paths, str):
            raw_interceptor_paths = [raw_interceptor_paths]
        elif not isinstance(raw_interceptor_paths, list):
            if raw_interceptor_paths is not None:
                logger.warning(f"mcpInterceptors must be a list of strings, got {type(raw_interceptor_paths).__name__}; skipping")
            raw_interceptor_paths = []
        for interceptor_path in raw_interceptor_paths:
            try:
                builder = resolve_variable(interceptor_path)
                interceptor = builder()
                if callable(interceptor):
                    tool_interceptors.append(interceptor)
                    logger.info(f"Loaded MCP interceptor: {interceptor_path}")
                elif interceptor is not None:
                    logger.warning(f"Builder {interceptor_path} returned non-callable {type(interceptor).__name__}; skipping")
            except Exception as e:
                logger.warning(f"Failed to load MCP interceptor {interceptor_path}: {e}", exc_info=True)

        client = MultiServerMCPClient(servers_config, tool_interceptors=tool_interceptors, tool_name_prefix=True)

        async def load_server_tools(server_name: str) -> list[BaseTool]:
            """Discover one server independently and retain its exact provenance."""
            try:
                server_config = extensions_config.mcp_servers.get(server_name)
                tool_name_prefix = server_config.tool_name_prefix if server_config is not None else True
                if tool_name_prefix:
                    return await client.get_tools(server_name=server_name)
                return await load_mcp_tools(
                    None,
                    connection=servers_config[server_name],
                    callbacks=getattr(client, "callbacks", None),
                    server_name=server_name,
                    tool_interceptors=getattr(client, "tool_interceptors", tool_interceptors),
                    tool_name_prefix=False,
                )
            except Exception as exc:
                logger.warning("Skipping MCP server '%s' after tool discovery failed: %s", server_name, exc, exc_info=True)
                return []

        server_names = list(servers_config)
        tools_by_server = await asyncio.gather(*(load_server_tools(name) for name in server_names))
        tools = [tool for server_tools in tools_by_server for tool in server_tools]
        logger.info(f"Successfully loaded {len(tools)} tool(s) from MCP servers")

        # A visible tool name must identify exactly one producer. This is mainly
        # relevant when a server disables prefixing; exclude every ambiguous
        # name instead of making routing depend on configuration order.
        tool_sources: dict[str, list[str]] = {}
        for source_name, server_tools in zip(server_names, tools_by_server, strict=True):
            for tool in server_tools:
                tool_sources.setdefault(tool.name, []).append(source_name)
        conflicting_names = {name for name, sources in tool_sources.items() if len(sources) > 1}
        for name in sorted(conflicting_names):
            logger.error(
                "Dropping ambiguous MCP tool '%s' exposed by servers: %s",
                name,
                ", ".join(tool_sources[name]),
            )

        wrapped_tools: list[BaseTool] = []
        for source_name, server_tools in zip(server_names, tools_by_server, strict=True):
            server_config = extensions_config.mcp_servers.get(source_name)
            tool_name_prefix = server_config.tool_name_prefix if server_config is not None else True
            for tool in server_tools:
                if tool.name in conflicting_names:
                    continue
                wrapped_tools.append(
                    _make_session_pool_tool(
                        tool,
                        source_name,
                        servers_config[source_name],
                        tool_interceptors,
                        tool_name_prefix=tool_name_prefix,
                    )
                )

        # Patch tools to support sync invocation, as deerflow client streams synchronously
        for tool in wrapped_tools:
            if getattr(tool, "func", None) is None and getattr(tool, "coroutine", None) is not None:
                tool.func = make_sync_tool_wrapper(tool.coroutine, tool.name)

        return wrapped_tools

    except Exception as e:
        logger.error(f"Failed to load MCP tools: {e}", exc_info=True)
        return []
