"""Neutralize framework control tokens in untrusted remote tool results."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace as dc_replace
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command


_REMOTE_CONTENT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "web_fetch",
        "web_search",
        "image_search",
        "web_capture",
    }
)


def _is_mcp_tool(tool: object) -> bool:
    metadata = getattr(tool, "metadata", None)
    if not isinstance(metadata, Mapping):
        return False
    return bool(metadata.get("mcp_server") or metadata.get("deerflow_mcp"))


def _neutralize_content(content: object) -> object:
    from deerflow.agents.middlewares.input_sanitization_middleware import (
        neutralize_untrusted_tags,
    )

    if isinstance(content, str):
        return neutralize_untrusted_tags(content)
    if isinstance(content, list):
        rebuilt: list[object] = []
        for block in content:
            if isinstance(block, str):
                rebuilt.append(neutralize_untrusted_tags(block))
            elif (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                rebuilt.append(
                    {**block, "text": neutralize_untrusted_tags(block["text"])}
                )
            else:
                rebuilt.append(block)
        return rebuilt
    return content


def _sanitize_tool_message(message: ToolMessage) -> ToolMessage:
    content = _neutralize_content(message.content)
    if content == message.content:
        return message
    return message.model_copy(update={"content": content})


def _sanitize_result(result: ToolMessage | Command) -> ToolMessage | Command:
    if isinstance(result, ToolMessage):
        return _sanitize_tool_message(result)

    update = getattr(result, "update", None)
    if not isinstance(update, dict):
        return result
    messages = update.get("messages")
    if not isinstance(messages, list):
        return result

    sanitized_messages: list[Any] = [
        _sanitize_tool_message(message)
        if isinstance(message, ToolMessage)
        else message
        for message in messages
    ]
    if sanitized_messages == messages:
        return result
    return dc_replace(result, update={**update, "messages": sanitized_messages})


class ToolResultSanitizationMiddleware(AgentMiddleware[AgentState]):
    """Sanitize first-party network and MCP-sourced tool results."""

    def _should_sanitize(self, request: ToolCallRequest) -> bool:
        if request.tool_call.get("name") in _REMOTE_CONTENT_TOOL_NAMES:
            return True
        return _is_mcp_tool(getattr(request, "tool", None))

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        result = handler(request)
        return _sanitize_result(result) if self._should_sanitize(request) else result

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest], Awaitable[ToolMessage | Command]
        ],
    ) -> ToolMessage | Command:
        result = await handler(request)
        return _sanitize_result(result) if self._should_sanitize(request) else result
