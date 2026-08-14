from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_UV_VERSION = "0.9.18"


def test_startup_scripts_sync_frozen_lock_then_disable_implicit_sync() -> None:
    for filename in ("start.sh", "start.bat"):
        text = (PROJECT_ROOT / filename).read_text(encoding="utf-8")
        assert "uv sync --frozen --inexact" in text
        run_lines = [line.strip() for line in text.splitlines() if "uv run" in line]
        assert run_lines
        assert all("uv run --no-sync" in line for line in run_lines)


def test_ci_pins_verified_uv_and_runs_without_second_sync() -> None:
    workflow = yaml.safe_load(
        (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["test"]["steps"]
    setup = next(step for step in steps if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"))
    assert setup["with"]["version"] == EXPECTED_UV_VERSION
    test_step = next(step for step in steps if step.get("name") == "Run test suite")
    assert test_step["run"] == "uv run --no-sync pytest -q"
