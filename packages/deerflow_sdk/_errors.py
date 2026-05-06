"""Public exception hierarchy.

All errors raised by the SDK inherit from ``DeerFlowError``.
Subclasses are stable; new subclasses may be added in minor versions.
"""

from __future__ import annotations


class DeerFlowError(Exception):
    """Base class for every error raised by the deerflow SDK."""


class ToolError(DeerFlowError):
    """A tool's ``__call__`` raised, returned an invalid value, or timed out.

    Attributes:
        tool_name: Name of the tool that failed.
        cause: Original exception, if any.
    """

    def __init__(self, tool_name: str, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(f"tool {tool_name!r}: {message}")
        self.tool_name = tool_name
        self.cause = cause


class PermissionDenied(DeerFlowError):
    """A ``Permission.check`` returned ``DENY`` for a tool call."""

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(f"permission denied for {tool_name!r}: {reason}")
        self.tool_name = tool_name
        self.reason = reason


class SandboxError(DeerFlowError):
    """The sandbox refused or failed to execute an operation.

    Examples: ``allow_host_bash=False`` blocked a shell command, docker
    container failed to start, file path escaped the sandbox root.
    """


class ModelError(DeerFlowError):
    """The underlying LLM provider returned an error or invalid response."""
