"""Structured state for one thread-scoped completion goal."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

GoalBlocker = Literal[
    "none",
    "missing_evidence",
    "needs_user_input",
    "run_failed",
    "external_wait",
    "goal_not_met_yet",
]


class GoalEvaluation(TypedDict):
    """One evaluator decision made after a visible agent turn."""

    satisfied: bool
    blocker: GoalBlocker
    reason: str
    evidence_summary: NotRequired[str]


class GoalState(TypedDict):
    """Persisted goal state stored in the thread checkpoint."""

    objective: str
    status: Literal["active"]
    created_at: str
    updated_at: str
    continuation_count: int
    max_continuations: int
    no_progress_count: int
    max_no_progress_continuations: int
    auto_continue: bool
    last_evaluation: NotRequired[dict[str, Any]]
