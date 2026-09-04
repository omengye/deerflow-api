from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import skills as skills_router
from deerflow.skills import SkillAlreadyExistsError


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(skills_router.router)
    return app


def test_skill_archive_upload_installs_from_bounded_temporary_file(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    class Client:
        def install_skill(self, archive_path: str | Path) -> dict[str, object]:
            path = Path(archive_path)
            observed["path"] = path
            observed["payload"] = path.read_bytes()
            return {
                "success": True,
                "skill_name": "demo",
                "message": "installed",
            }

    monkeypatch.setattr(
        skills_router,
        "get_client_manager",
        lambda: SimpleNamespace(get_client=lambda: Client()),
    )

    response = TestClient(_app()).post(
        "/skills/install/upload",
        files={"archive": ("demo.skill", b"archive-bytes", "application/zip")},
    )

    assert response.status_code == 200
    assert response.json()["skill_name"] == "demo"
    assert observed["payload"] == b"archive-bytes"
    assert not Path(observed["path"]).exists()  # type: ignore[arg-type]


def test_skill_archive_upload_rejects_wrong_extension(monkeypatch) -> None:
    monkeypatch.setattr(
        skills_router,
        "get_client_manager",
        lambda: (_ for _ in ()).throw(AssertionError("manager must not be used")),
    )

    response = TestClient(_app()).post(
        "/skills/install/upload",
        files={"archive": ("demo.zip", b"archive-bytes", "application/zip")},
    )

    assert response.status_code == 400
    assert ".skill" in response.json()["detail"]


def test_skill_archive_upload_enforces_size_limit(monkeypatch) -> None:
    monkeypatch.setattr(skills_router, "_MAX_SKILL_ARCHIVE_UPLOAD_BYTES", 4)
    monkeypatch.setattr(
        skills_router,
        "get_client_manager",
        lambda: (_ for _ in ()).throw(AssertionError("manager must not be used")),
    )

    response = TestClient(_app()).post(
        "/skills/install/upload",
        files={"archive": ("demo.skill", b"12345", "application/zip")},
    )

    assert response.status_code == 413


def test_skill_archive_upload_maps_duplicate_to_conflict(monkeypatch) -> None:
    class Client:
        def install_skill(self, _archive_path):
            raise SkillAlreadyExistsError("Skill 'demo' already exists")

    monkeypatch.setattr(
        skills_router,
        "get_client_manager",
        lambda: SimpleNamespace(get_client=lambda: Client()),
    )

    response = TestClient(_app()).post(
        "/skills/install/upload",
        files={"archive": ("demo.skill", b"archive-bytes", "application/zip")},
    )

    assert response.status_code == 409
