"""Sandbox protocol.

A ``Sandbox`` is an isolated execution surface for tools that touch the
filesystem or run shell commands. The SDK ships ``LocalSandbox`` (with
``allow_host_bash`` enforcement) and is designed so users can plug in
``DockerSandbox``, ``E2BSandbox``, etc.

Concrete implementations MUST be async-safe and re-entrant within the
same harness instance.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class ExecResult(BaseModel):
    """Result of ``Sandbox.execute``."""

    model_config = ConfigDict(extra="forbid")

    stdout: str
    stderr: str
    exit_code: int


@runtime_checkable
class Sandbox(Protocol):
    """Async sandbox protocol."""

    async def execute(self, command: str, *, timeout: int = 60) -> ExecResult:
        """Run ``command`` in the sandbox shell.

        Implementations MUST raise ``SandboxError`` if the command is
        denied by policy (e.g. ``allow_host_bash=False``).
        """
        ...

    async def read_file(self, path: str) -> str: ...
    async def write_file(self, path: str, content: str) -> None: ...
    async def list_dir(self, path: str) -> list[str]: ...

    async def close(self) -> None:
        """Release sandbox resources. Idempotent."""
        ...
