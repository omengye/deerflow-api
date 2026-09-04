from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "public"
    / "podcast-generation"
    / "scripts"
    / "generate.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("podcast_generate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("speaker", "env_name", "expected_default"),
    [
        ("male", "VOLCENGINE_TTS_VOICE_TYPE_MALE", "zh_male_yangguangqingnian_moon_bigtts"),
        ("female", "VOLCENGINE_TTS_VOICE_TYPE_FEMALE", "zh_female_sajiaonvyou_moon_bigtts"),
    ],
)
@pytest.mark.parametrize("configured", [None, "", "   ", "  custom-voice  "])
def test_volcengine_voice_can_be_overridden(
    monkeypatch,
    speaker: str,
    env_name: str,
    expected_default: str,
    configured: str | None,
) -> None:
    module = _load_module()
    monkeypatch.delenv(env_name, raising=False)
    if configured is not None:
        monkeypatch.setenv(env_name, configured)
    seen: list[str] = []
    monkeypatch.setattr(
        module,
        "text_to_speech",
        lambda _text, voice: seen.append(voice) or b"audio",
    )

    module._process_line((0, module.ScriptLine(speaker=speaker, paragraph="hi"), 1))

    assert seen == [configured.strip() if configured and configured.strip() else expected_default]
