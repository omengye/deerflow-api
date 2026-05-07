"""Canonical serialization for LangChain / LangGraph objects.

Provides a single source of truth for converting LangChain message
objects, Pydantic models, and LangGraph state dicts into plain
JSON-serialisable Python structures.

Consumers: ``deerflow.runtime.runs.worker`` (SSE publishing) and
``app.gateway.routers.threads`` (REST responses).
"""

from __future__ import annotations

from typing import Any


def serialize_lc_object(obj: Any) -> Any:
    """Recursively serialize a LangChain object to a JSON-serialisable dict."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: serialize_lc_object(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [serialize_lc_object(item) for item in obj]
    # Pydantic v2
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    # Pydantic v1 / older objects
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    # Last resort
    try:
        return str(obj)
    except Exception:
        return repr(obj)


def serialize_channel_values(channel_values: dict[str, Any]) -> dict[str, Any]:
    """Serialize channel values, stripping internal LangGraph keys.

    Internal keys like ``__pregel_*`` and ``__interrupt__`` are removed
    to match what the LangGraph Platform API returns.
    """
    result: dict[str, Any] = {}
    for key, value in channel_values.items():
        if key.startswith("__pregel_") or key == "__interrupt__":
            continue
        result[key] = serialize_lc_object(value)
    return result


def serialize_messages_tuple(obj: Any) -> Any:
    """Serialize a messages-mode tuple ``(chunk, metadata)``.

    LangChain's ``model_dump()`` does not surface ``additional_kwargs``
    sub-fields (e.g. ``reasoning_content``) as top-level keys in the
    serialised dict.  We lift them manually so that downstream consumers
    (the AG-UI adapter in ``chat.py``) can find ``reasoning_content``
    at the top level of the chunk dict.
    """
    if isinstance(obj, tuple) and len(obj) == 2:
        chunk, metadata = obj
        serialized_chunk = serialize_lc_object(chunk)
        # Lift reasoning_content from additional_kwargs to top level
        if isinstance(serialized_chunk, dict):
            additional_kwargs = serialized_chunk.get("additional_kwargs")
            if isinstance(additional_kwargs, dict):
                reasoning = additional_kwargs.get("reasoning_content")
                if reasoning and not serialized_chunk.get("reasoning_content"):
                    serialized_chunk["reasoning_content"] = reasoning
        return [serialized_chunk, metadata if isinstance(metadata, dict) else {}]
    return serialize_lc_object(obj)


def serialize(obj: Any, *, mode: str = "") -> Any:
    """Serialize LangChain objects with mode-specific handling.

    * ``messages`` — obj is ``(message_chunk, metadata_dict)``
    * ``values`` — obj is the full state dict; ``__pregel_*`` keys stripped
    * everything else — recursive ``model_dump()`` / ``dict()`` fallback
    """
    if mode == "messages":
        return serialize_messages_tuple(obj)
    if mode == "values":
        return serialize_channel_values(obj) if isinstance(obj, dict) else serialize_lc_object(obj)
    return serialize_lc_object(obj)
