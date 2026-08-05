"""Convert durable user/assistant conversation turns into mem0 messages."""

from __future__ import annotations

from typing import Any

from deerflow.agents.memory.message_processing import extract_message_text

_TASK_SCOPED_MARKERS = (
    "task_scoped",
    "_task_scoped",
    "_deerflow_task_scoped",
    "subagent_internal",
)


def to_mem0_messages(messages: list[Any]) -> list[dict[str, str]]:
    converted: list[dict[str, str]] = []
    for message in messages:
        additional = getattr(message, "additional_kwargs", {}) or {}
        if any(bool(additional.get(key)) for key in _TASK_SCOPED_MARKERS):
            continue
        message_type = getattr(message, "type", None)
        if message_type not in {"human", "ai"}:
            continue
        if message_type == "ai" and getattr(message, "tool_calls", None):
            continue
        content = extract_message_text(message).strip()
        if not content:
            continue
        converted.append(
            {"role": "user" if message_type == "human" else "assistant", "content": content}
        )
    return converted

