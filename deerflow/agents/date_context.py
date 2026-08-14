"""Shared runtime date context for lead agents and subagents."""

from datetime import datetime


def get_current_date() -> str:
    """Return the local calendar date used to anchor agent reasoning."""
    return datetime.now().strftime("%Y-%m-%d, %A")


def append_current_date(prompt: str, current_date: str | None = None) -> str:
    """Append a framework-owned date tag to a system prompt."""
    value = current_date or get_current_date()
    return prompt + f"\n<current_date>{value}</current_date>"
