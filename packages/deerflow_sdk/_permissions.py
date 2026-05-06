"""Permission gate API.

A ``Permission`` decides whether a tool call is allowed *before* it runs.
Multiple permissions chain with AND semantics: the call proceeds only if
every permission returns ``ALLOW``. The first non-``ALLOW`` short-circuits.

This is distinct from ``Hook.pre_tool_use``: hooks observe, permissions
gate. For human-in-the-loop approval, return ``ASK_USER`` and implement
the prompting in ``ctx.ask_user``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK_USER = "ask_user"


# A user-supplied async callable that prompts a human for approval.
# Returning True ⇒ ALLOW, False ⇒ DENY.
AskUserFn = Callable[[str, str, dict[str, Any]], Awaitable[bool]]


class PermissionContext(BaseModel):
    """Context handed to every permission check.

    ``ask_user`` is provided by the harness when a UI is wired up; if not,
    returning ``ASK_USER`` falls back to ``DENY`` for safety.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run_id: str
    thread_id: str
    user_data: dict[str, Any] = Field(default_factory=dict)
    ask_user: AskUserFn | None = None


class Permission:
    """Subclass and override ``check``."""

    async def check(
        self,
        ctx: PermissionContext,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> PermissionDecision:
        return PermissionDecision.ALLOW
