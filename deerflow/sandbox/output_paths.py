"""Helpers for resolving the virtual outputs directory to host paths."""

from pathlib import Path

from deerflow.config.paths import VIRTUAL_PATH_PREFIX

OUTPUTS_VIRTUAL_PREFIX = f"{VIRTUAL_PATH_PREFIX}/outputs"


def workspace_outputs_path(workspace_path: str) -> str:
    """Return the host outputs directory associated with an external workspace."""

    return str(Path(workspace_path) / "outputs")


def resolve_outputs_virtual_path(outputs_path: str, virtual_path: str) -> Path:
    """Resolve an outputs virtual path beneath an explicit host outputs root."""

    stripped = virtual_path.lstrip("/")
    prefix = OUTPUTS_VIRTUAL_PREFIX.lstrip("/")
    if stripped != prefix and not stripped.startswith(prefix + "/"):
        raise ValueError(f"Path must start with {OUTPUTS_VIRTUAL_PREFIX}")

    relative = stripped[len(prefix) :].lstrip("/")
    outputs_dir = Path(outputs_path).expanduser().resolve()
    actual = (outputs_dir / relative).resolve()
    try:
        actual.relative_to(outputs_dir)
    except ValueError as exc:
        raise ValueError("Access denied: path traversal detected") from exc
    return actual
