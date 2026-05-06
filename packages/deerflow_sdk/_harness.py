"""``Harness`` — the main user-facing entry point.

A ``Harness`` encapsulates everything needed to run an agent: model,
tools, sub-agents, hooks, permissions, sandbox, checkpointer.

ZERO module-level state. Two ``Harness`` instances in the same process
are fully independent — different models, different sandboxes, different
checkpointers, different running tasks.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, TypeVar, overload

from pydantic import BaseModel

from deerflow_sdk._config import HarnessConfig, ModelConfig
from deerflow_sdk._events import StreamEvent
from deerflow_sdk._hooks import Hook
from deerflow_sdk._permissions import AskUserFn, Permission
from deerflow_sdk._sandbox.base import Sandbox
from deerflow_sdk._subagents import SubagentSpec
from deerflow_sdk._tools import Tool

T = TypeVar("T", bound=BaseModel)


class Harness:
    """An isolated agent runtime.

    Args:
        model: Model name (e.g. ``"qwen3.6-plus"``) or a ``ModelConfig``.
        tools: Tools available to the lead agent.
        subagents: Sub-agent specs the lead agent can dispatch.
        hooks: Observers for the agent loop.
        permissions: Pre-tool-use gates. AND-chained.
        sandbox: Optional sandbox shared by all tools that need one.
        system_prompt: System prompt for the lead agent.
        max_iterations: Hard cap on agent loop iterations.
        config: Alternative to keyword args; if given, other kwargs MUST be omitted.

    Examples:
        Simple::

            harness = Harness(model="qwen3.6-plus", tools=[get_weather])
            answer = await harness.run("Weather in Shanghai?")

        Structured output::

            class Report(BaseModel): summary: str
            report = await harness.run("Summarise this", output_type=Report)

        Streaming::

            async for event in harness.stream("Plan a trip"):
                ...

    Lifecycle::

        async with Harness(...) as h:
            await h.run("...")
        # sandbox closed, checkpointer flushed
    """

    def __init__(
        self,
        *,
        model: str | ModelConfig | None = None,
        tools: list[Tool] | None = None,
        subagents: list[SubagentSpec] | None = None,
        hooks: list[Hook] | None = None,
        permissions: list[Permission] | None = None,
        ask_user: AskUserFn | None = None,
        sandbox: Sandbox | None = None,
        system_prompt: str | None = None,
        max_iterations: int = 50,
        config: HarnessConfig | None = None,
    ) -> None:
        if config is not None and model is not None:
            raise TypeError("pass either `config=` or `model=`, not both")
        if config is None:
            if model is None:
                raise TypeError("`model` is required when `config` is not given")
            config = HarnessConfig(
                model=model,
                system_prompt=system_prompt,
                max_iterations=max_iterations,
            )
        # ------------------------------------------------------------------
        # All state lives here. NO module-level globals.
        # ------------------------------------------------------------------
        self._config: HarnessConfig = config
        self._tools: tuple[Tool, ...] = tuple(tools or ())
        self._subagents: tuple[SubagentSpec, ...] = tuple(subagents or ())
        self._hooks: tuple[Hook, ...] = tuple(hooks or ())
        self._permissions: tuple[Permission, ...] = tuple(permissions or ())
        self._ask_user: AskUserFn | None = ask_user
        self._sandbox: Sandbox | None = sandbox

        # Engine is constructed lazily so that contract tests can build a
        # Harness without a configured model provider.
        self._engine: Any | None = None
        self._closed = False

    # ----------------------- public API ------------------------------------

    @overload
    async def run(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
        output_type: None = None,
    ) -> str: ...

    @overload
    async def run(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
        output_type: type[T],
    ) -> T: ...

    async def run(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
        output_type: type[T] | None = None,
    ) -> T | str:
        """Run the agent to completion and return the final output.

        If ``output_type`` is given, the model is constrained to produce
        that schema and the result is the parsed pydantic instance.
        Otherwise, the result is the assistant's final text.
        """
        engine = self._ensure_engine()
        result = await engine.run(prompt, thread_id=thread_id, output_type=output_type)
        if output_type is None:
            if not isinstance(result, str):
                raise TypeError("engine returned structured output without output_type")
            return result
        if not isinstance(result, output_type):
            raise TypeError(f"engine returned {type(result).__name__}, expected {output_type.__name__}")
        return result

    async def stream(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream typed events for an agent run.

        The final event is always a ``RunComplete``.
        """
        engine = self._ensure_engine()
        async for event in engine.stream(prompt, thread_id=thread_id):
            yield event

    async def aclose(self) -> None:
        """Release engine, sandbox, and checkpointer resources. Idempotent."""
        if self._closed:
            return
        if self._sandbox is not None:
            await self._sandbox.close()
        self._closed = True

    async def __aenter__(self) -> "Harness":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ----------------------- introspection ---------------------------------

    @property
    def config(self) -> HarnessConfig:
        return self._config

    @property
    def tools(self) -> tuple[Tool, ...]:
        return self._tools

    @property
    def subagents(self) -> tuple[SubagentSpec, ...]:
        return self._subagents

    def _ensure_engine(self) -> Any:
        if self._closed:
            raise RuntimeError("Harness is closed")
        if self._engine is None:
            from deerflow_sdk._engine.langgraph_engine import LangGraphEngine

            self._engine = LangGraphEngine(
                config=self._config,
                tools=self._tools,
                subagents=self._subagents,
                hooks=self._hooks,
                permissions=self._permissions,
                ask_user=self._ask_user,
                sandbox=self._sandbox,
            )
        return self._engine
