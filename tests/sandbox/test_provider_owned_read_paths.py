from pathlib import Path
from types import SimpleNamespace

import pytest

from deerflow.sandbox import tools as sandbox_tools
from deerflow.sandbox.local.local_sandbox import LocalSandbox, PathMapping
from deerflow.sandbox.tools import glob_tool, grep_tool, ls_tool, read_file_tool


def _runtime(tmp_path: Path, projection: Path) -> SimpleNamespace:
    workspace = tmp_path / "workspace"
    uploads = tmp_path / "uploads"
    outputs = tmp_path / "outputs"
    for directory in (workspace, uploads, outputs):
        directory.mkdir()
    return SimpleNamespace(
        context={"thread_id": "provider-owned"},
        config={},
        state={
            "sandbox": {
                "sandbox_id": "local:provider-owned",
                "skills_path": str(projection),
            },
            "thread_data": {
                "thread_id": "provider-owned",
                "workspace_path": str(workspace),
                "uploads_path": str(uploads),
                "outputs_path": str(outputs),
            },
        },
    )


def test_skill_reads_keep_acquired_provider_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = tmp_path / "acquired-projection"
    skill_dir = projection / "public" / "enabled-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# enabled-skill\nprovider-owned marker\n", encoding="utf-8")

    sandbox = LocalSandbox(
        "local:provider-owned",
        path_mappings=[
            PathMapping(
                container_path="/mnt/skills",
                local_path=str(projection),
                read_only=True,
            )
        ],
    )
    runtime = _runtime(tmp_path, projection)
    monkeypatch.setattr(sandbox_tools, "ensure_sandbox_initialized", lambda _runtime: sandbox)

    def _must_not_reconstruct(*_args, **_kwargs):
        raise AssertionError("mounted reads must remain provider-owned")

    monkeypatch.setattr(sandbox_tools, "_resolve_skills_path", _must_not_reconstruct)
    root = "/mnt/skills/public/enabled-skill"
    file_path = f"{root}/SKILL.md"

    assert read_file_tool.func(runtime, description="read skill", path=file_path) == (
        "# enabled-skill\nprovider-owned marker\n"
    )
    assert file_path in ls_tool.func(runtime, description="list skill", path=root)
    assert file_path in glob_tool.func(runtime, description="find skill", pattern="*.md", path=root)
    assert file_path in grep_tool.func(
        runtime,
        description="search skill",
        pattern="provider-owned marker",
        path=root,
        literal=True,
    )
