from __future__ import annotations

import hashlib
from pathlib import Path

import acp
import httpx
import pytest

from deerflow.acp.artifact_publisher import ArtifactPublishError, RustFSArtifactPublisher
from deerflow.acp.config import LocalACPArtifactConfig
from deerflow.tools.builtins.acp_artifact_downloader import ACPArtifactDownloader


class _FakePaths:
    def __init__(self, outputs: Path) -> None:
        self.outputs = outputs

    def resolve_virtual_path(self, session_id: str, virtual_path: str) -> Path:
        assert session_id == "session-1"
        prefix = "/mnt/user-data/outputs/"
        if not virtual_path.startswith(prefix):
            raise ValueError("outside outputs")
        return self.outputs / virtual_path.removeprefix(prefix)

    def sandbox_outputs_dir(self, session_id: str) -> Path:
        assert session_id == "session-1"
        return self.outputs


class _FakeS3:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, str, dict]] = []

    def upload_file(self, path: str, bucket: str, key: str, *, ExtraArgs: dict) -> None:
        self.uploads.append((path, bucket, key, ExtraArgs))

    def generate_presigned_url(self, operation: str, *, Params: dict, ExpiresIn: int) -> str:
        assert operation == "get_object"
        assert ExpiresIn == 900
        return f"https://rustfs.example.test/{Params['Bucket']}/{Params['Key']}?signed=1"


def _artifact_config(**overrides) -> LocalACPArtifactConfig:
    values = {
        "endpoint_url": "https://rustfs.example.test",
        "bucket": "acp",
        "access_key": "access",
        "secret_key": "secret",
    }
    values.update(overrides)
    return LocalACPArtifactConfig(**values)


@pytest.mark.asyncio
async def test_rustfs_publisher_uploads_presented_output_and_emits_signed_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    content = b"artifact contents"
    artifact = outputs / "report.txt"
    artifact.write_bytes(content)
    fake_s3 = _FakeS3()
    monkeypatch.setattr(
        "deerflow.acp.artifact_publisher.get_paths",
        lambda: _FakePaths(outputs),
    )
    publisher = RustFSArtifactPublisher(
        _artifact_config(),
        client_factory=lambda: fake_s3,
    )

    block = await publisher.publish(
        "session-1",
        "run-1",
        "/mnt/user-data/outputs/report.txt",
    )

    digest = hashlib.sha256(content).hexdigest()
    assert block.type == "resource_link"
    assert block.uri.startswith("https://rustfs.example.test/acp/acp-artifacts/session-1/run-1/")
    assert block.field_meta == {
        "deerflow": {
            "sha256": digest,
            "objectKey": f"acp-artifacts/session-1/run-1/{digest}-report.txt",
        }
    }
    assert fake_s3.uploads[0][1] == "acp"
    assert fake_s3.uploads[0][3]["Metadata"] == {"sha256": digest}


@pytest.mark.asyncio
async def test_rustfs_publisher_rejects_oversized_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "large.bin").write_bytes(b"12345")
    monkeypatch.setattr(
        "deerflow.acp.artifact_publisher.get_paths",
        lambda: _FakePaths(outputs),
    )
    publisher = RustFSArtifactPublisher(
        _artifact_config(max_file_size_bytes=4),
        client_factory=_FakeS3,
    )

    with pytest.raises(ArtifactPublishError, match="maximum"):
        await publisher.publish(
            "session-1",
            "run-1",
            "/mnt/user-data/outputs/large.bin",
        )


@pytest.mark.asyncio
async def test_remote_downloader_verifies_and_places_artifact_in_acp_workspace(
    tmp_path: Path,
) -> None:
    content = b"downloaded artifact"
    digest = hashlib.sha256(content).hexdigest()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "rustfs.example.test"
        return httpx.Response(200, content=content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = ACPArtifactDownloader(
            tmp_path,
            "invocation-1",
            allowed_hosts=["rustfs.example.test"],
            max_bytes=1024,
            timeout_seconds=5,
            http_client=client,
        )
        block = acp.resource_link_block(
            "report.txt",
            "https://rustfs.example.test/acp/report.txt?signed=1",
            mime_type="text/plain",
            size=len(content),
        )
        block.field_meta = {"deerflow": {"sha256": digest}}

        result = await downloader.download(block)

    assert result.virtual_path == "/mnt/acp-workspace/invocation-1/report.txt"
    assert result.sha256 == digest
    assert (tmp_path / "invocation-1" / "report.txt").read_bytes() == content


@pytest.mark.asyncio
async def test_remote_downloader_rejects_unapproved_host(tmp_path: Path) -> None:
    downloader = ACPArtifactDownloader(
        tmp_path,
        "invocation-1",
        allowed_hosts=["rustfs.example.test"],
        max_bytes=1024,
        timeout_seconds=5,
    )
    block = acp.resource_link_block(
        "secret.txt",
        "https://untrusted.example.test/secret.txt",
    )

    with pytest.raises(ValueError, match="not allowed"):
        await downloader.download(block)


@pytest.mark.asyncio
async def test_remote_downloader_rejects_http_by_default(tmp_path: Path) -> None:
    downloader = ACPArtifactDownloader(
        tmp_path,
        "invocation-1",
        allowed_hosts=["192.168.1.190:9000"],
        max_bytes=1024,
        timeout_seconds=5,
    )
    block = acp.resource_link_block(
        "report.txt",
        "http://192.168.1.190:9000/acp/report.txt?signed=1",
    )

    with pytest.raises(ValueError, match="artifact_allow_insecure_http"):
        await downloader.download(block)


@pytest.mark.asyncio
async def test_remote_downloader_allows_http_for_explicitly_approved_host(
    tmp_path: Path,
) -> None:
    content = b"private network artifact"
    digest = hashlib.sha256(content).hexdigest()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "192.168.1.190"
        assert request.url.port == 9000
        return httpx.Response(200, content=content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloader = ACPArtifactDownloader(
            tmp_path,
            "invocation-1",
            allowed_hosts=["192.168.1.190:9000"],
            allow_insecure_http=True,
            max_bytes=1024,
            timeout_seconds=5,
            http_client=client,
        )
        block = acp.resource_link_block(
            "report.txt",
            "http://192.168.1.190:9000/acp/report.txt?signed=1",
            size=len(content),
        )
        block.field_meta = {"deerflow": {"sha256": digest}}

        result = await downloader.download(block)

    assert result.virtual_path == "/mnt/acp-workspace/invocation-1/report.txt"
    assert result.sha256 == digest
    assert (tmp_path / "invocation-1" / "report.txt").read_bytes() == content


@pytest.mark.asyncio
async def test_remote_downloader_http_still_requires_approved_host(tmp_path: Path) -> None:
    downloader = ACPArtifactDownloader(
        tmp_path,
        "invocation-1",
        allowed_hosts=["192.168.1.190:9000"],
        allow_insecure_http=True,
        max_bytes=1024,
        timeout_seconds=5,
    )
    block = acp.resource_link_block(
        "secret.txt",
        "http://192.168.1.191:9000/secret.txt",
    )

    with pytest.raises(ValueError, match="not allowed"):
        await downloader.download(block)
