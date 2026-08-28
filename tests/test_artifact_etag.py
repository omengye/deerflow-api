from __future__ import annotations

import hashlib
from types import SimpleNamespace

from app.routers import uploads


class _ArtifactClient:
    def __init__(self, content: bytes, mime_type: str = "text/plain") -> None:
        self.content = content
        self.mime_type = mime_type

    def get_artifact(self, thread_id: str, path: str) -> tuple[bytes, str]:
        assert thread_id == "thread-1"
        assert path == "mnt/user-data/outputs/report.txt"
        return self.content, self.mime_type


async def test_artifact_response_includes_sha256_etag(monkeypatch) -> None:
    content = b"artifact contents"
    client = _ArtifactClient(content)
    monkeypatch.setattr(
        uploads,
        "get_client_manager",
        lambda: SimpleNamespace(get_client=lambda: client),
    )

    response = await uploads.get_artifact(
        "thread-1",
        "mnt/user-data/outputs/report.txt",
    )

    expected = hashlib.sha256(content).hexdigest()
    assert response.headers["etag"] == f'"{expected}"'
    assert response.body == content
    assert "content-disposition" not in response.headers


async def test_download_keeps_content_disposition_and_sha256_etag(monkeypatch) -> None:
    content = b"download me"
    client = _ArtifactClient(content)
    monkeypatch.setattr(
        uploads,
        "get_client_manager",
        lambda: SimpleNamespace(get_client=lambda: client),
    )

    response = await uploads.get_artifact(
        "thread-1",
        "mnt/user-data/outputs/report.txt",
        download=True,
    )

    expected = hashlib.sha256(content).hexdigest()
    assert response.headers["etag"] == f'"{expected}"'
    assert response.headers["content-disposition"].startswith("attachment;")
