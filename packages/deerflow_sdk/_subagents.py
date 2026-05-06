"""Sub-agent definition API.

Sub-agents are independently-configured agents that the lead agent can
dispatch as tools. The decorator pattern produces a ``SubagentSpec``,
which the harness compiles into the appropriate engine primitive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from deerflow_sdk._config import ModelConfig
from deerflow_sdk._tools import Tool


@dataclass(frozen=True)
class SubagentSpec:
    """Declarative spec for a sub-agent.

    The harness builds a ``dispatch_<name>`` tool that the parent can call.
    Sub-agents have their own model, tools, system prompt, and iteration
    budget — they are *not* a thin wrapper around the parent.
    """

    name: str
    description: str
    tools: tuple[Tool, ...] = ()
    model: str | ModelConfig | None = None
    system_prompt: str | None = None
    max_iterations: int = 30
    extra: dict[str, Any] = field(default_factory=dict)


def subagent(
    *,
    name: str,
    description: str,
    tools: list[Tool] | None = None,
    model: str | ModelConfig | None = None,
    system_prompt: str | None = None,
    max_iterations: int = 30,
) -> Callable[[type], SubagentSpec]:
    """Class decorator that turns a class into a ``SubagentSpec``.

    Example:
        @subagent(name="researcher", description="Web research expert.",
                  tools=[web_search, web_fetch], model="claude-opus-4-7")
        class Researcher:
            system_prompt = "You are a meticulous researcher..."

    The decorated symbol becomes the ``SubagentSpec`` itself; the original
    class body is consumed for ``system_prompt`` if not given as kwarg.
    """

    def _wrap(cls: type) -> SubagentSpec:
        actual_prompt = system_prompt or getattr(cls, "system_prompt", None)
        return SubagentSpec(
            name=name,
            description=description,
            tools=tuple(tools or ()),
            model=model,
            system_prompt=actual_prompt,
            max_iterations=max_iterations,
        )

    return _wrap
