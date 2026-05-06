"""Hook API.

Hooks observe the agent loop. They cannot modify control flow; for
that, use ``Permission`` (gate tool execution) or ``output_type``
(constrain model output).

A subclass overrides any subset of the ``on_*`` / ``pre_*`` / ``post_*``
methods. Default implementations are no-ops so subclasses stay small.

Hooks may raise. The engine catches the exception, surfaces it as a
``ToolError`` event in the stream, and continues the run unless the hook
sets ``ctx.abort = True``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HookContext(BaseModel):
    """Mutable context object passed to each hook callback.

    Set ``abort = True`` from any hook to terminate the run cleanly.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run_id: str
    thread_id: str
    user_data: dict[str, Any] = Field(default_factory=dict)
    abort: bool = False


class Hook:
    """Base class for hooks. All methods are optional and default to no-op."""

    async def on_run_start(self, ctx: HookContext, prompt: str) -> None:
        """Called once before the first model invocation."""

    async def on_user_prompt(self, ctx: HookContext, prompt: str) -> str | None:
        """Called for every user message. Return a string to *replace* the prompt."""
        return None

    async def pre_tool_use(
        self, ctx: HookContext, tool_name: str, tool_input: dict[str, Any]
    ) -> None:
        """Called just before a tool executes."""

    async def post_tool_use(
        self,
        ctx: HookContext,
        tool_name: str,
        tool_input: dict[str, Any],
        output: Any,
        error: Exception | None,
    ) -> None:
        """Called after a tool finishes (success or failure)."""

    async def on_run_end(self, ctx: HookContext, final_output: Any) -> None:
        """Called once after the last model response."""
