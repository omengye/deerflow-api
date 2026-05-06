"""Streaming event types — framework-owned, NOT leaked from langgraph.

Users iterate over ``Harness.stream(...)`` and receive these typed events.
The ``type`` field is a discriminator suitable for ``match`` / pydantic
discriminated unions.

Adding new event subclasses is a backwards-compatible change.
Renaming or removing fields on existing subclasses is a breaking change.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StreamEvent(BaseModel):
    """Base class for all streaming events.

    Each concrete subclass declares its own ``type: Literal[...]`` field which
    acts as the discriminator. The base class intentionally does not declare
    ``type`` so that subclasses can use invariant ``Literal`` types without
    triggering variance errors from strict type checkers.
    """

    model_config = ConfigDict(extra="forbid")


class TextDelta(StreamEvent):
    """An incremental chunk of assistant text."""

    type: Literal["text_delta"] = "text_delta"
    delta: str


class ToolCall(StreamEvent):
    """The agent decided to call a tool."""

    type: Literal["tool_call"] = "tool_call"
    tool_name: str
    tool_call_id: str
    input: dict[str, Any]


class ToolResult(StreamEvent):
    """A tool finished executing.

    ``error`` is set if the tool raised; ``output`` is set otherwise.
    """

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    tool_name: str
    output: Any | None = None
    error: str | None = None


class SubagentStart(StreamEvent):
    """A sub-agent run has started inside the parent."""

    type: Literal["subagent_start"] = "subagent_start"
    subagent_name: str
    run_id: str
    prompt: str


class SubagentEnd(StreamEvent):
    """A sub-agent run finished."""

    type: Literal["subagent_end"] = "subagent_end"
    subagent_name: str
    run_id: str
    output: Any | None = None
    error: str | None = None


class TokenUsage(BaseModel):
    """Cumulative token counts for a run."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class RunComplete(StreamEvent):
    """Terminal event of a ``stream()``. Always the last event."""

    type: Literal["run_complete"] = "run_complete"
    final_output: Any
    usage: TokenUsage = Field(default_factory=TokenUsage)
