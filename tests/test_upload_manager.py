"""Regression tests for collision-safe upload landing."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from deerflow.client import DeerFlowClient
from deerflow.config.paths import Paths
from deerflow.uploads import manager as upload_manager
from deerflow.utils import file_conversion


def test_max_length_filename_dedup_stays_within_utf8_limit() -> None:
    name = f"{'a' * 251}.txt"

    deduplicated = upload_manager.claim_unique_filename(name, {name})

    assert deduplicated.endswith("_1.txt")
    assert len(deduplicated.encode("utf-8")) <= 255
    assert upload_manager.normalize_filename(deduplicated) == deduplicated


def test_multibyte_filename_dedup_truncates_on_codepoint_boundary() -> None:
    name = f"{'界' * 83}a.txt"

    deduplicated = upload_manager.claim_unique_filename(name, {name})

    assert deduplicated.endswith("_1.txt")
    assert "�" not in deduplicated
    assert len(deduplicated.encode("utf-8")) <= 255
    assert upload_manager.normalize_filename(deduplicated) == deduplicated


def test_repeated_max_length_collisions_remain_unique_and_valid() -> None:
    name = f"{'a' * 251}.txt"
    seen = {name}

    generated = [upload_manager.claim_unique_filename(name, seen) for _ in range(12)]

    assert len(set(generated)) == len(generated)
    assert all(len(candidate.encode("utf-8")) <= 255 for candidate in generated)
    assert generated[-1].endswith("_12.txt")


def test_client_upload_preserves_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upload_manager, "get_paths", lambda: Paths(tmp_path))
    uploads_dir = Paths(tmp_path).sandbox_uploads_dir("thread-1")
    uploads_dir.mkdir(parents=True)
    (uploads_dir / "report.txt").write_text("existing", encoding="utf-8")
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "report.txt"
    source.write_text("new", encoding="utf-8")

    result = DeerFlowClient.__new__(DeerFlowClient).upload_files("thread-1", [source])

    assert (uploads_dir / "report.txt").read_text(encoding="utf-8") == "existing"
    assert (uploads_dir / "report_1.txt").read_text(encoding="utf-8") == "new"
    assert result["files"][0]["filename"] == "report_1.txt"
    assert result["files"][0]["original_filename"] == "report.txt"


def test_concurrent_copies_claim_distinct_names(tmp_path: Path) -> None:
    destination = tmp_path / "uploads"
    destination.mkdir()
    source = tmp_path / "sample.txt"
    source.write_text("payload", encoding="utf-8")

    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(lambda _: upload_manager.copy_file_exclusive(source, destination), range(2)))

    assert {path.name for path in paths} == {"sample.txt", "sample_1.txt"}
    assert all(path.read_text(encoding="utf-8") == "payload" for path in paths)


def test_existing_symlink_is_never_followed(tmp_path: Path) -> None:
    destination = tmp_path / "uploads"
    destination.mkdir()
    source = tmp_path / "sample.txt"
    source.write_text("new", encoding="utf-8")
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    try:
        (destination / "sample.txt").symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable on this platform: {exc}")

    copied = upload_manager.copy_file_exclusive(source, destination)

    assert copied.name == "sample_1.txt"
    assert target.read_text(encoding="utf-8") == "outside"


async def test_conversion_preserves_existing_markdown_companion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"pdf")
    existing = tmp_path / "report.md"
    existing.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(file_conversion, "_get_pdf_converter", lambda: "auto")
    monkeypatch.setattr(file_conversion, "_do_convert", lambda _path, _converter: "converted")

    converted = await file_conversion.convert_file_to_markdown(source)

    assert converted == tmp_path / "report_1.md"
    assert existing.read_text(encoding="utf-8") == "existing"
    assert converted.read_text(encoding="utf-8") == "converted"
