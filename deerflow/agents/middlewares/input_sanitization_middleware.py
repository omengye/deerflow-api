"""Temporary model-bound sanitization for genuine user input.

The middleware keeps framework authority tags distinguishable from text a user
typed by escaping only the reserved XML-like tag names.  It also frames the
genuine user turn with stable plain-text boundaries.  No transformed content is
written back to graph state or checkpoints.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphBubbleUp

logger = logging.getLogger(__name__)

# Framework-authored authority blocks used by this project, plus common tags
# used to make untrusted text look like higher-priority model instructions.
_BLOCKED_TAG_NAMES = frozenset(
    {
        "analysis",
        "available-deferred-tools",
        "available_skills",
        "citations",
        "clarification_system",
        "critical_reminders",
        "current_date",
        "current_uploads",
        "developer",
        "file_editing_workflow",
        "guidelines",
        "important",
        "instruction",
        "local_acp_context",
        "memory",
        "output_format",
        "override",
        "prompt",
        "relevant_memory",
        "response_style",
        "role",
        "skill_system",
        "soul",
        "subagent_system",
        "system",
        "system-reminder",
        "system_reminder",
        "think",
        "thinking_style",
        "todo_list_system",
        "uploaded_files",
        "working_directory",
    }
)

_BLOCKED_TAG_PATTERN = re.compile(
    r"<\s*/?\s*(?:"
    + "|".join(re.escape(tag) for tag in sorted(_BLOCKED_TAG_NAMES))
    + r")\b[^>]*>?",
    re.IGNORECASE,
)

_USER_INPUT_BEGIN = "--- BEGIN USER INPUT ---"
_USER_INPUT_END = "--- END USER INPUT ---"
_BOUNDARY_PATTERN = re.compile(
    f"{re.escape(_USER_INPUT_BEGIN)}|{re.escape(_USER_INPUT_END)}"
)


def _escape_reserved_tags(text: str) -> str:
    return _BLOCKED_TAG_PATTERN.sub(
        lambda match: match.group(0).replace("<", "&lt;").replace(">", "&gt;"),
        text,
    )


def _neutralize_boundaries(text: str) -> str:
    return _BOUNDARY_PATTERN.sub(
        lambda match: "[BEGIN USER INPUT]"
        if match.group(0) == _USER_INPUT_BEGIN
        else "[END USER INPUT]",
        text,
    )


def sanitize_user_text(text: str) -> str:
    """Escape reserved tags and frame one plain-text user payload."""
    if not text.strip():
        return text

    escaped = _escape_reserved_tags(text)
    if (
        escaped.startswith(_USER_INPUT_BEGIN)
        and escaped.endswith(_USER_INPUT_END)
        and escaped.count(_USER_INPUT_BEGIN) == 1
        and escaped.count(_USER_INPUT_END) == 1
    ):
        return escaped

    escaped = _neutralize_boundaries(escaped)
    return f"{_USER_INPUT_BEGIN}\n{escaped}\n{_USER_INPUT_END}"


def is_genuine_user_message(message: object) -> bool:
    """Exclude HumanMessages synthesized by framework middlewares/providers."""
    if not isinstance(message, HumanMessage):
        return False
    if message.name:
        return False
    kwargs = message.additional_kwargs or {}
    return not (
        kwargs.get("hide_from_ui")
        or kwargs.get("_view_image_injection")
        or kwargs.get("lc_source") in {"summarization", "summarization_fallback"}
    )


def _text_entries(content: list[Any]) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    for index, block in enumerate(content):
        if isinstance(block, str):
            entries.append((index, block))
        elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            entries.append((index, block["text"]))
    return entries


def _sanitize_list_content(content: list[Any]) -> list[Any] | None:
    """Sanitize text blocks without moving interleaved multimodal blocks."""
    entries = _text_entries(content)
    nonblank = [(index, text) for index, text in entries if text.strip()]
    if not nonblank:
        return None

    first_index = nonblank[0][0]
    last_index = nonblank[-1][0]
    all_text = "\n".join(text for _, text in entries)
    already_wrapped = (
        nonblank[0][1].startswith(_USER_INPUT_BEGIN)
        and nonblank[-1][1].endswith(_USER_INPUT_END)
        and all_text.count(_USER_INPUT_BEGIN) == 1
        and all_text.count(_USER_INPUT_END) == 1
    )

    changed = False
    updated = list(content)
    for index, text in entries:
        processed = _escape_reserved_tags(text)
        if not already_wrapped:
            processed = _neutralize_boundaries(processed)
            if index == first_index:
                processed = f"{_USER_INPUT_BEGIN}\n{processed}"
            if index == last_index:
                processed = f"{processed}\n{_USER_INPUT_END}"
        if processed == text:
            continue
        changed = True
        block = content[index]
        updated[index] = processed if isinstance(block, str) else {**block, "text": processed}

    return updated if changed else None


class InputSanitizationMiddleware(AgentMiddleware[AgentState]):
    """Sanitize every genuine HumanMessage in the temporary model view.

    The transformation is deliberately not checkpointed.  Reprocessing the
    full genuine-user history on every call therefore matters: after a later
    user turn is appended, earlier raw messages must not silently re-enter the
    model context without the same trust-boundary framing.
    """

    def _process_request(self, request: ModelRequest) -> ModelRequest:
        messages = list(request.messages)
        changed = False
        for index, message in enumerate(messages):
            if not is_genuine_user_message(message):
                continue

            content = message.content
            if isinstance(content, str):
                processed: str | list[Any] = sanitize_user_text(content)
                if processed == content:
                    continue
            elif isinstance(content, list):
                sanitized = _sanitize_list_content(content)
                if sanitized is None:
                    continue
                processed = sanitized
            else:
                continue

            messages[index] = message.model_copy(update={"content": processed})
            changed = True
        return request.override(messages=messages) if changed else request

    def _try_process(self, request: ModelRequest) -> ModelRequest:
        try:
            return self._process_request(request)
        except GraphBubbleUp:
            raise
        except Exception:
            logger.warning("Input sanitization failed; using the original model request", exc_info=True)
            return request

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._try_process(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._try_process(request))
