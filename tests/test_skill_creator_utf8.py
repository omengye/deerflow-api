from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_CREATOR_ROOT = REPO_ROOT / "skills" / "public" / "skill-creator"


def _load_validator():
    path = SKILL_CREATOR_ROOT / "scripts" / "quick_validate.py"
    spec = importlib.util.spec_from_file_location("skill_creator_quick_validate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_quick_validate_reports_invalid_utf8_without_raising(tmp_path: Path) -> None:
    validator = _load_validator()
    skill_dir = tmp_path / "invalid"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_bytes(
        b"---\nname: invalid\ndescription: \xff\n---\n"
    )

    assert validator.validate_skill(skill_dir) == (
        False,
        "SKILL.md is not valid UTF-8",
    )


def test_skill_creator_text_file_io_declares_utf8() -> None:
    missing: list[str] = []
    for path in sorted(SKILL_CREATOR_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            operation = None
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "read_text",
                "write_text",
            }:
                operation = node.func.attr
            elif isinstance(node.func, ast.Name) and node.func.id == "open":
                mode = (
                    node.args[1]
                    if len(node.args) > 1
                    else next(
                        (kw.value for kw in node.keywords if kw.arg == "mode"),
                        None,
                    )
                )
                if isinstance(mode, ast.Constant) and "b" in str(mode.value):
                    continue
                operation = "open"
            if operation is None:
                continue
            encoding = next(
                (kw.value for kw in node.keywords if kw.arg == "encoding"),
                None,
            )
            if not (
                isinstance(encoding, ast.Constant)
                and encoding.value == "utf-8"
            ):
                missing.append(f"{path.relative_to(SKILL_CREATOR_ROOT)}:{node.lineno} {operation}")

    assert missing == []
