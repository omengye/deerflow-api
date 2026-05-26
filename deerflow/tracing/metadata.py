"""Langfuse trace metadata construction.

The ``langfuse-langchain`` callback handler reads a small set of reserved
keys from ``RunnableConfig.metadata`` (``langfuse_session_id``,
``langfuse_user_id``, ``langfuse_trace_name``, ``langfuse_tags``). This
module centralises how those keys are populated so the gateway worker and
any embedded clients produce identical, well-tagged traces.

When Langfuse is not enabled, ``build_langfuse_trace_metadata`` returns an
empty dict and ``inject_langfuse_metadata`` is a no-op.
"""

from __future__ import annotations

from typing import Any

from deerflow.config import get_enabled_tracing_providers

_LANGFUSE_KEYS = (
    "langfuse_session_id",
    "langfuse_user_id",
    "langfuse_trace_name",
    "langfuse_tags",
)


def build_langfuse_trace_metadata(
    *,
    thread_id: str | None,
    user_id: str | None,
    assistant_id: str | None,
    model_name: str | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """Build the Langfuse metadata payload for a single run.

    Returns ``{}`` when Langfuse is not in the enabled tracing providers,
    so callers can unconditionally merge the result into ``config.metadata``.
    """
    if "langfuse" not in get_enabled_tracing_providers():
        return {}

    metadata: dict[str, Any] = {}
    if thread_id:
        metadata["langfuse_session_id"] = thread_id
    if user_id:
        metadata["langfuse_user_id"] = user_id

    metadata["langfuse_trace_name"] = assistant_id or "lead-agent"

    tags: list[str] = []
    if environment:
        tags.append(f"env:{environment}")
    if model_name:
        tags.append(f"model:{model_name}")
    if tags:
        metadata["langfuse_tags"] = tags

    return metadata


def inject_langfuse_metadata(
    config: dict[str, Any],
    *,
    thread_id: str | None,
    user_id: str | None,
    assistant_id: str | None,
    model_name: str | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """Merge Langfuse trace metadata into ``config['metadata']`` (in-place).

    Existing values for any of the reserved keys win — callers can preset
    a specific ``langfuse_trace_name`` and have it preserved.
    """
    payload = build_langfuse_trace_metadata(
        thread_id=thread_id,
        user_id=user_id,
        assistant_id=assistant_id,
        model_name=model_name,
        environment=environment,
    )
    if not payload:
        return config

    config_metadata = config.setdefault("metadata", {})
    for key in _LANGFUSE_KEYS:
        if key in payload:
            config_metadata.setdefault(key, payload[key])
    return config
