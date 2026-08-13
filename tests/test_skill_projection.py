from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import deerflow.config.paths as paths_module
import deerflow.skills.projection as projection_module
from deerflow.sandbox.local.local_sandbox_provider import LocalSandboxProvider
from deerflow.sandbox.tools import validate_local_bash_command_paths
from deerflow.skills.projection import get_skill_projection, prune_skill_projections


def _skill(root: Path, category: str, directory: str, name: str, body: str) -> None:
    target = root / category / directory
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test {name}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    (target / "references").mkdir()
    (target / "references" / "details.md").write_text(body, encoding="utf-8")


@pytest.fixture
def projection_config(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    state_root = tmp_path / "state"
    _skill(skills_root, "public", "enabled-dir", "enabled-skill", "enabled")
    _skill(skills_root, "custom", "disabled-dir", "disabled-skill", "disabled")
    _skill(skills_root, "custom", "other-dir", "other-skill", "other")
    extensions = tmp_path / "extensions.json"
    extensions.write_text(
        '{"skills":{"enabled-skill":{"enabled":true},"disabled-skill":{"enabled":false},"other-skill":{"enabled":true}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("DEER_FLOW_EXTENSIONS_CONFIG_PATH", str(extensions))
    monkeypatch.setenv("DEER_FLOW_HOME", str(state_root))
    paths_module._paths = None
    projection_module._projection_cache.clear()
    projection_module._last_projection_gc = 0.0
    config = SimpleNamespace(
        skills=SimpleNamespace(
            enabled=True,
            get_skills_path=lambda: skills_root,
            container_path="/mnt/skills",
        ),
        sandbox=SimpleNamespace(mounts=[]),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    yield skills_root, state_root, config
    projection_module._projection_cache.clear()
    projection_module._last_projection_gc = 0.0
    paths_module._paths = None


def test_projection_contains_only_enabled_allowlisted_skills(projection_config) -> None:
    skills_root, _, _ = projection_config

    projection = get_skill_projection({"enabled-skill", "disabled-skill"})

    enabled = projection.path / "public" / "enabled-dir" / "SKILL.md"
    disabled = projection.path / "custom" / "disabled-dir" / "SKILL.md"
    other = projection.path / "custom" / "other-dir" / "SKILL.md"
    assert enabled.read_text(encoding="utf-8").endswith("enabled\n")
    assert (projection.path / "public" / "enabled-dir" / "references" / "details.md").is_file()
    assert not disabled.exists()
    assert not other.exists()
    assert projection.path != skills_root
    assert projection.skill_names == frozenset({"enabled-skill"})


def test_projection_revision_changes_when_skill_content_changes(projection_config) -> None:
    skills_root, _, _ = projection_config
    first = get_skill_projection({"enabled-skill"})

    (skills_root / "public" / "enabled-dir" / "references" / "details.md").write_text(
        "updated",
        encoding="utf-8",
    )
    second = get_skill_projection({"enabled-skill"})

    assert first.revision != second.revision
    assert first.path != second.path
    assert (second.path / "public" / "enabled-dir" / "references" / "details.md").read_text(encoding="utf-8") == "updated"


def test_unchanged_projection_reuses_cached_content_hash(projection_config) -> None:
    first = get_skill_projection({"enabled-skill"})

    with patch.object(
        projection_module,
        "_projection_revision",
        wraps=projection_module._projection_revision,
    ) as hash_revision:
        second = get_skill_projection({"enabled-skill"})

    assert second == first
    hash_revision.assert_not_called()


def test_projection_gc_keeps_newest_and_explicit_revisions(projection_config) -> None:
    _, state_root, _ = projection_config
    root = state_root / "skill-projections"
    root.mkdir(parents=True)
    old = root / ("1" * 24)
    explicit = root / ("2" * 24)
    newest = root / ("3" * 24)
    for index, path in enumerate((old, explicit, newest), start=1):
        path.mkdir()
        (path / ".projection.json").write_text("{}", encoding="utf-8")
        projection_module.os.utime(path, (index, index))

    removed = prune_skill_projections(
        keep_revisions={explicit.name},
        now=100,
        retention_seconds=1,
        min_keep=1,
    )

    assert removed == [old.name]
    assert not old.exists()
    assert explicit.exists()
    assert newest.exists()


def test_projection_rescans_metadata_and_bytes_during_publish_race(
    projection_config,
) -> None:
    skills_root, _, _ = projection_config
    source = skills_root / "public" / "enabled-dir" / "references" / "details.md"
    original_copyfile = projection_module.shutil.copyfile
    mutated = False

    def mutate_before_first_copy(src, dst, *args, **kwargs):
        nonlocal mutated
        if not mutated:
            mutated = True
            source.write_text("published-during-projection", encoding="utf-8")
        return original_copyfile(src, dst, *args, **kwargs)

    with patch.object(
        projection_module.shutil,
        "copyfile",
        side_effect=mutate_before_first_copy,
    ):
        raced = get_skill_projection({"enabled-skill"})

    stable = get_skill_projection({"enabled-skill"})

    assert mutated is True
    assert raced.revision == stable.revision
    assert raced.path == stable.path
    assert (
        raced.path / "public" / "enabled-dir" / "references" / "details.md"
    ).read_text(encoding="utf-8") == "published-during-projection"


def test_projection_does_not_publish_stale_allowlist_metadata(
    projection_config,
) -> None:
    skills_root, _, _ = projection_config
    source = skills_root / "public" / "enabled-dir" / "SKILL.md"
    original_copyfile = projection_module.shutil.copyfile
    mutated = False

    def rename_before_first_copy(src, dst, *args, **kwargs):
        nonlocal mutated
        if not mutated:
            mutated = True
            source.write_text(
                "---\nname: renamed-skill\ndescription: renamed\n---\n\nnew\n",
                encoding="utf-8",
            )
        return original_copyfile(src, dst, *args, **kwargs)

    with patch.object(
        projection_module.shutil,
        "copyfile",
        side_effect=rename_before_first_copy,
    ):
        projection = get_skill_projection({"enabled-skill"})

    assert mutated is True
    assert projection.skill_names == frozenset()
    assert not (projection.path / "public" / "enabled-dir" / "SKILL.md").exists()


def test_local_provider_cache_is_revision_and_allowlist_scoped(projection_config) -> None:
    provider = LocalSandboxProvider()

    enabled_id = provider.acquire("thread-1", available_skills={"enabled-skill"})
    other_id = provider.acquire("thread-1", available_skills={"other-skill"})
    enabled = provider.get(enabled_id)
    other = provider.get(other_id)

    assert enabled_id != other_id
    assert enabled is not None and other is not None
    assert Path(enabled._resolve_path("/mnt/skills/public/enabled-dir/SKILL.md")).is_file()
    assert not Path(enabled._resolve_path("/mnt/skills/custom/other-dir/SKILL.md")).exists()
    assert Path(other._resolve_path("/mnt/skills/custom/other-dir/SKILL.md")).is_file()
    assert not Path(other._resolve_path("/mnt/skills/public/enabled-dir/SKILL.md")).exists()
    assert enabled._is_read_only_path(enabled._resolve_path("/mnt/skills/public/enabled-dir/SKILL.md"))


@pytest.mark.parametrize(
    "command",
    [
        "ls /mnt/skills",
        "cat /mnt/skills/public/example/SKILL.md",
        'cat "/mnt/skills/public/example/SKILL.md"',
        "cat '/mnt/skills/public/example/SKILL.md'",
        "echo corrupted >/mnt/skills/x",
        "SKILLS=/mnt/skills; chmod -R u+w \"$SKILLS\"",
    ],
)
def test_host_bash_cannot_access_content_addressed_skill_projection(command: str) -> None:
    with pytest.raises(PermissionError, match="Host bash cannot access /mnt/skills"):
        validate_local_bash_command_paths(command, {})


def test_host_bash_skill_guard_does_not_match_similar_absolute_prefix() -> None:
    with pytest.raises(PermissionError, match="Unsafe absolute paths") as exc_info:
        validate_local_bash_command_paths("cat /mnt/skills-other/file", {})

    assert "Host bash cannot access /mnt/skills" not in str(exc_info.value)


def test_host_bash_skill_guard_does_not_match_url_path_text() -> None:
    validate_local_bash_command_paths("curl https://example.test/mnt/skills/readme", {})
