"""Local sandbox — runs commands on the host machine.

CRITICAL: Unlike the legacy ``deerflow.sandbox.local.LocalSandbox`` (which
relied on the *tool registration* layer to gate ``host_bash``), this v0.1
class enforces ``allow_host_bash`` **inside ``execute()`` itself**. Direct
calls to ``execute()`` cannot bypass the gate.

Default is ``allow_host_bash=False`` — secure by default.
"""

from __future__ import annotations

import os
from pathlib import Path

from deerflow_sdk._errors import SandboxError
from deerflow_sdk._sandbox.base import ExecResult


class LocalSandbox:
    """Local-process sandbox with explicit shell-execution gating.

    Args:
        root: Optional chroot-like directory. File operations are confined
            to this directory if set; ``..`` traversal raises ``SandboxError``.
            ``None`` means unrestricted (use only in dev).
        allow_host_bash: If ``False`` (default), ``execute()`` raises
            ``SandboxError`` regardless of the command. Read/write file
            operations remain available.
    """

    def __init__(
        self,
        *,
        root: str | os.PathLike[str] | None = None,
        allow_host_bash: bool = False,
    ) -> None:
        self._root: Path | None = Path(root).resolve() if root is not None else None
        self._allow_host_bash = allow_host_bash
        if self._root is not None:
            self._root.mkdir(parents=True, exist_ok=True)

    async def execute(self, command: str, *, timeout: int = 60) -> ExecResult:
        if not self._allow_host_bash:
            raise SandboxError(
                "host bash execution is disabled. "
                "Pass allow_host_bash=True to LocalSandbox if you really mean this."
            )
        # Implementation deferred to Phase 3.
        raise NotImplementedError("LocalSandbox.execute not implemented yet")

    async def read_file(self, path: str) -> str:
        raise NotImplementedError("LocalSandbox.read_file not implemented yet")

    async def write_file(self, path: str, content: str) -> None:
        raise NotImplementedError("LocalSandbox.write_file not implemented yet")

    async def list_dir(self, path: str) -> list[str]:
        raise NotImplementedError("LocalSandbox.list_dir not implemented yet")

    async def close(self) -> None:
        # Nothing to release for the local sandbox.
        return None

    # ------------------------------------------------------------------
    # Internal helpers (private, exposed only to tests in same package).
    # ------------------------------------------------------------------

    def _resolve_inside_root(self, path: str) -> Path:
        if self._root is None:
            return Path(path).resolve()
        candidate = (self._root / path).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise SandboxError(f"path {path!r} escapes sandbox root") from exc
        return candidate
