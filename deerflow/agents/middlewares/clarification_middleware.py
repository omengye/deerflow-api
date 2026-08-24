"""Middleware for intercepting clarification requests and presenting them to the user."""

import json
import logging
import re
from collections.abc import Callable
from hashlib import sha256
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

logger = logging.getLogger(__name__)

ASK_CLARIFICATION_TOOL_NAME = "ask_clarification"
_XML_TAG_RE = re.compile(r"</?[A-Za-z_][\w:.-]*(?:\s[^<>]*?)?\s*/?>")


def _filter_provider_tool_blocks(
    content: Any,
    kept_ids: set[str],
    kept_names: set[str],
) -> Any:
    """Keep provider-native content blocks aligned with structured tool calls."""
    if not isinstance(content, list):
        return content

    filtered: list[Any] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in {"tool_use", "function_call"}:
            block_id = block.get("id")
            if isinstance(block_id, str) and block_id:
                if block_id not in kept_ids:
                    continue
            elif block.get("type") == "function_call":
                name = block.get("name")
                if not isinstance(name, str) or name not in kept_names:
                    continue
        filtered.append(block)
    return filtered


def _clone_with_tool_calls(
    message: AIMessage,
    tool_calls: list[dict[str, Any]],
    *,
    content: Any,
) -> AIMessage:
    """Clone an AI message while synchronizing raw provider tool metadata."""
    kept_ids = {
        call["id"]
        for call in tool_calls
        if isinstance(call.get("id"), str) and call["id"]
    }
    kept_names = {
        str(call["name"])
        for call in tool_calls
        if isinstance(call.get("name"), str) and call["name"]
    }

    additional_kwargs = dict(message.additional_kwargs or {})
    raw_tool_calls = additional_kwargs.get("tool_calls")
    if isinstance(raw_tool_calls, list):
        retained_raw = [
            raw
            for raw in raw_tool_calls
            if isinstance(raw, dict)
            and isinstance(raw.get("id"), str)
            and raw["id"] in kept_ids
        ]
        if retained_raw:
            additional_kwargs["tool_calls"] = retained_raw
        else:
            additional_kwargs.pop("tool_calls", None)

    raw_function_call = additional_kwargs.get("function_call")
    if isinstance(raw_function_call, dict):
        if raw_function_call.get("name") not in kept_names:
            additional_kwargs.pop("function_call", None)
    elif not tool_calls:
        additional_kwargs.pop("function_call", None)

    response_metadata = dict(message.response_metadata or {})
    if not tool_calls and response_metadata.get("finish_reason") == "tool_calls":
        response_metadata["finish_reason"] = "stop"

    return message.model_copy(
        update={
            "content": content,
            "tool_calls": tool_calls,
            "additional_kwargs": additional_kwargs,
            "response_metadata": response_metadata,
        }
    )


class ClarificationMiddlewareState(AgentState):
    """Compatible with the `ThreadState` schema."""

    pass


class ClarificationMiddleware(AgentMiddleware[ClarificationMiddlewareState]):
    """Intercepts clarification tool calls and interrupts execution to present questions to the user.

    When the model calls the `ask_clarification` tool, this middleware:
    1. Intercepts the tool call before execution
    2. Extracts the clarification question and metadata
    3. Formats a user-friendly message
    4. Returns a Command that interrupts execution and presents the question
    5. Waits for user response before continuing

    This replaces the tool-based approach where clarification continued the conversation flow.
    """

    state_schema = ClarificationMiddlewareState

    def _drop_parallel_non_clarification_tools(
        self,
        state: AgentState,
    ) -> dict | None:
        """Keep only clarification calls when a provider batches sibling tools.

        Tool nodes may execute parallel calls before a ``return_direct`` result
        is routed to the end of the graph.  Rewriting the final AI message in
        ``after_model`` prevents a sibling write or command from running before
        the user has answered the clarification.
        """
        messages = list(state.get("messages") or [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return None

        message = messages[-1]
        tool_calls = list(message.tool_calls or [])
        invalid_tool_calls = [
            call
            for call in (getattr(message, "invalid_tool_calls", None) or [])
            if isinstance(call, dict)
        ]
        clarification_calls = [
            call
            for call in tool_calls
            if call.get("name") == ASK_CLARIFICATION_TOOL_NAME
        ]
        invalid_clarification_calls = [
            call
            for call in invalid_tool_calls
            if call.get("name") == ASK_CLARIFICATION_TOOL_NAME
        ]
        if not clarification_calls and not invalid_clarification_calls:
            return None

        sibling_calls = [
            call
            for call in tool_calls
            if call.get("name") != ASK_CLARIFICATION_TOOL_NAME
        ]
        if not sibling_calls:
            return None

        logger.warning(
            "ask_clarification was emitted with sibling tool call(s); dropping %s",
            [str(call.get("name") or "unknown") for call in sibling_calls],
        )
        kept_for_content = clarification_calls + invalid_clarification_calls
        kept_ids = {
            call["id"]
            for call in kept_for_content
            if isinstance(call.get("id"), str) and call["id"]
        }
        kept_names = {
            str(call["name"])
            for call in kept_for_content
            if isinstance(call.get("name"), str) and call["name"]
        }
        filtered_content = _filter_provider_tool_blocks(
            message.content,
            kept_ids,
            kept_names,
        )
        patched = _clone_with_tool_calls(
            message,
            clarification_calls,
            content=filtered_content,
        )
        return {"messages": [patched]}

    def _stable_message_id(self, tool_call_id: str, formatted_message: str) -> str:
        """Build a deterministic message ID so retried clarification calls replace, not append."""
        if tool_call_id:
            return f"clarification:{tool_call_id}"
        digest = sha256(formatted_message.encode("utf-8")).hexdigest()[:16]
        return f"clarification:{digest}"

    def _is_chinese(self, text: str) -> bool:
        """Check if text contains Chinese characters.

        Args:
            text: Text to check

        Returns:
            True if text contains Chinese characters
        """
        return any("\u4e00" <= char <= "\u9fff" for char in text)

    @staticmethod
    def _flatten_dict_option_values(value: dict[str, Any]) -> list[str | int | float]:
        """Flatten scalar leaves from XML-to-dict payloads in source order."""
        flattened: list[str | int | float] = []

        def collect(nested: Any) -> None:
            if isinstance(nested, dict):
                for item in nested.values():
                    collect(item)
            elif isinstance(nested, list):
                for item in nested:
                    collect(item)
            elif isinstance(nested, str | int | float):
                flattened.append(nested)

        collect(value)
        return flattened

    def _normalize_options(self, options: Any) -> list[str]:
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except (json.JSONDecodeError, TypeError):
                options = [options]
        if options is None:
            return []
        if isinstance(options, dict):
            options = self._flatten_dict_option_values(options)
        elif not isinstance(options, list):
            options = [options]

        normalized: list[str] = []
        seen: set[str] = set()
        for option in options:
            text = _XML_TAG_RE.sub("", str(option)).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        return normalized

    def _format_clarification_message(self, args: dict) -> str:
        """Format the clarification arguments into a user-friendly message.

        Args:
            args: The tool call arguments containing clarification details

        Returns:
            Formatted message string
        """
        question = args.get("question", "")
        clarification_type = args.get("clarification_type", "missing_info")
        context = args.get("context")
        options = args.get("options", [])

        options = self._normalize_options(options)

        # Type-specific icons
        type_icons = {
            "missing_info": "❓",
            "ambiguous_requirement": "🤔",
            "approach_choice": "🔀",
            "risk_confirmation": "⚠️",
            "suggestion": "💡",
        }

        icon = type_icons.get(clarification_type, "❓")

        # Build the message naturally
        message_parts = []

        # Add icon and question together for a more natural flow
        if context:
            # If there's context, present it first as background
            message_parts.append(f"{icon} {context}")
            message_parts.append(f"\n{question}")
        else:
            # Just the question with icon
            message_parts.append(f"{icon} {question}")

        # Add options in a cleaner format
        if options and len(options) > 0:
            message_parts.append("")  # blank line for spacing
            for i, option in enumerate(options, 1):
                message_parts.append(f"  {i}. {option}")

        return "\n".join(message_parts)

    def _handle_clarification(self, request: ToolCallRequest) -> Command:
        """Handle clarification request and return command to interrupt execution.

        Args:
            request: Tool call request

        Returns:
            Command that interrupts execution with the formatted clarification message
        """
        # Extract clarification arguments
        args = request.tool_call.get("args", {})
        question = args.get("question", "")

        logger.info("Intercepted clarification request")
        logger.debug("Clarification question: %s", question)

        # Format the clarification message
        formatted_message = self._format_clarification_message(args)

        # Get the tool call ID
        tool_call_id = request.tool_call.get("id", "")

        # Create a ToolMessage with the formatted question
        # This will be added to the message history
        tool_message = ToolMessage(
            id=self._stable_message_id(tool_call_id, formatted_message),
            content=formatted_message,
            tool_call_id=tool_call_id,
            name="ask_clarification",
        )

        # Return a Command that:
        # 1. Adds the formatted tool message
        # 2. Interrupts execution by going to __end__
        # Note: We don't add an extra AIMessage here - the frontend will detect
        # and display ask_clarification tool messages directly
        return Command(
            update={"messages": [tool_message]},
            goto=END,
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Intercept ask_clarification tool calls and interrupt execution (sync version).

        Args:
            request: Tool call request
            handler: Original tool execution handler

        Returns:
            Command that interrupts execution with the formatted clarification message
        """
        # Check if this is an ask_clarification tool call
        if request.tool_call.get("name") != ASK_CLARIFICATION_TOOL_NAME:
            # Not a clarification call, execute normally
            return handler(request)

        return self._handle_clarification(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Intercept ask_clarification tool calls and interrupt execution (async version).

        Args:
            request: Tool call request
            handler: Original tool execution handler (async)

        Returns:
            Command that interrupts execution with the formatted clarification message
        """
        # Check if this is an ask_clarification tool call
        if request.tool_call.get("name") != ASK_CLARIFICATION_TOOL_NAME:
            # Not a clarification call, execute normally
            return await handler(request)

        return self._handle_clarification(request)

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._drop_parallel_non_clarification_tools(state)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._drop_parallel_non_clarification_tools(state)
