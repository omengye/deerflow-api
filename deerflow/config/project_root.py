"""Project-root discovery helpers for the embedded DeerFlow API package."""

from pathlib import Path


def find_project_root(start: Path | None = None) -> Path:
    """Find the nearest project root containing this embedded harness.

    The original DeerFlow harness lived under a monorepo layout. This API
    package embeds the harness directly, so path discovery must be based on
    local project markers instead of fixed parent counts.
    """
    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / "deerflow").is_dir() and (
            (candidate / "config.yaml").exists()
            or (candidate / "config.example.yaml").exists()
            or (candidate / "pyproject.toml").exists()
        ):
            return candidate

    # Fallback for the embedded package layout:
    # deerflow/config/project_root.py -> project root is parents[2].
    return Path(__file__).resolve().parents[2]
