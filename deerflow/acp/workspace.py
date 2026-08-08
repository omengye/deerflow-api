"""Workspace path validation for the local ACP adapter."""

from __future__ import annotations

import os
from pathlib import Path


def normalize_workspace_cwd(cwd: str) -> str:
    """Return a canonical, existing directory for an ACP session ``cwd``.

    ACP requires session working directories to be absolute.  The local
    adapter additionally resolves symlinks/junctions up front so every later
    file-operation boundary check uses one stable root.
    """

    if not isinstance(cwd, str) or not cwd.strip():
        raise ValueError("ACP cwd must be a non-empty absolute directory path")
    if "\x00" in cwd or any(ord(char) < 32 for char in cwd):
        raise ValueError("ACP cwd contains control characters")

    path = Path(cwd).expanduser()
    if not path.is_absolute():
        raise ValueError("ACP cwd must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"ACP cwd does not exist: {cwd}") from exc
    except OSError as exc:
        raise ValueError(f"ACP cwd cannot be resolved: {cwd}") from exc
    if not resolved.is_dir():
        raise ValueError(f"ACP cwd is not a directory: {cwd}")
    return str(resolved)


def workspace_paths_equal(left: str, right: str) -> bool:
    """Compare canonical workspace paths with native platform semantics."""

    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(
        os.path.normpath(right)
    )
