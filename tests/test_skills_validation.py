from pathlib import Path

import pytest

from deerflow.skills.validation import _validate_skill_frontmatter


@pytest.mark.parametrize("description", ["''", "'   '"])
def test_skill_frontmatter_rejects_blank_description(
    tmp_path: Path,
    description: str,
) -> None:
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: demo\ndescription: {description}\n---\n\n# Demo\n",
        encoding="utf-8",
    )

    valid, message, name = _validate_skill_frontmatter(skill_dir)

    assert valid is False
    assert "empty" in message.lower()
    assert name is None
