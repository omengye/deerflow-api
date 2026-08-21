"""Publish explicitly presented local ACP artifacts to S3-compatible storage."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from acp import schema

from deerflow.config.paths import get_paths
from deerflow.sandbox.output_paths import resolve_outputs_virtual_path

from .config import LocalACPArtifactConfig

_OUTPUT_PREFIX = "/mnt/user-data/outputs/"
_SAFE_KEY_PART = re.compile(r"[^a-zA-Z0-9._-]+")


class ArtifactPublishError(RuntimeError):
    """Raised when an artifact cannot be safely published."""


def _safe_key_part(value: str, fallback: str) -> str:
    normalized = _SAFE_KEY_PART.sub("-", value).strip(".-")
    return normalized[:160] or fallback


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RustFSArtifactPublisher:
    """Upload ACP outputs to a private RustFS/S3 bucket and return signed links."""

    def __init__(
        self,
        config: LocalACPArtifactConfig,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory or self._build_client
        self._client: Any = None

    def _build_client(self) -> Any:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise ArtifactPublishError(
                "RustFS artifact publishing requires the 'rustfs' extra: "
                "install deerflow-api[rustfs]"
            ) from exc
        return boto3.client(
            "s3",
            endpoint_url=self.config.endpoint_url,
            aws_access_key_id=self.config.access_key,
            aws_secret_access_key=self.config.secret_key,
            region_name=self.config.region,
            verify=self.config.verify_ssl,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": self.config.addressing_style},
            ),
        )

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _resolve(
        self,
        session_id: str,
        virtual_path: str,
        outputs_path: str | None = None,
    ) -> tuple[Path, str, str, int]:
        if not virtual_path.startswith(_OUTPUT_PREFIX):
            raise ArtifactPublishError(
                f"Only files under {_OUTPUT_PREFIX} can be published: {virtual_path}"
            )
        try:
            if outputs_path is None:
                host_path = get_paths().resolve_virtual_path(
                    session_id,
                    virtual_path,
                ).resolve()
                outputs_dir = get_paths().sandbox_outputs_dir(session_id).resolve()
            else:
                outputs_dir = Path(outputs_path).resolve()
                host_path = resolve_outputs_virtual_path(
                    str(outputs_dir),
                    virtual_path,
                )
            host_path.relative_to(outputs_dir)
        except (OSError, ValueError) as exc:
            raise ArtifactPublishError(f"Artifact path is invalid: {virtual_path}") from exc
        if not host_path.is_file():
            raise ArtifactPublishError(f"Artifact file does not exist: {virtual_path}")
        size = host_path.stat().st_size
        if size > self.config.max_file_size_bytes:
            raise ArtifactPublishError(
                f"Artifact is {size} bytes; maximum is {self.config.max_file_size_bytes} bytes"
            )
        name = PurePosixPath(virtual_path).name or "artifact"
        mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return host_path, name, mime_type, size

    def _publish_sync(
        self,
        session_id: str,
        run_id: str,
        virtual_path: str,
        outputs_path: str | None = None,
    ) -> schema.ResourceContentBlock:
        host_path, name, mime_type, size = self._resolve(
            session_id,
            virtual_path,
            outputs_path,
        )
        before = host_path.stat()
        sha256 = _sha256(host_path)
        key = "/".join(
            (
                self.config.prefix,
                _safe_key_part(session_id, "session"),
                _safe_key_part(run_id, "run"),
                f"{sha256}-{_safe_key_part(name, 'artifact')}",
            )
        )
        client = self._get_client()
        try:
            client.upload_file(
                str(host_path),
                self.config.bucket,
                key,
                ExtraArgs={
                    "ContentType": mime_type,
                    "Metadata": {"sha256": sha256},
                },
            )
            after = host_path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ArtifactPublishError(
                    f"Artifact changed while it was being uploaded: {virtual_path}"
                )
            uri = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.config.bucket, "Key": key},
                ExpiresIn=self.config.presigned_get_expires_seconds,
            )
        except ArtifactPublishError:
            raise
        except Exception as exc:
            raise ArtifactPublishError(
                f"RustFS upload failed for {name}: {type(exc).__name__}: {exc}"
            ) from exc
        return schema.ResourceContentBlock(
            type="resource_link",
            name=name,
            uri=str(uri),
            mime_type=mime_type,
            size=size,
            description="DeerFlow ACP artifact published through RustFS",
            field_meta={
                "deerflow": {
                    "sha256": sha256,
                    "objectKey": key,
                }
            },
        )

    async def publish(
        self,
        session_id: str,
        run_id: str,
        virtual_path: str,
        outputs_path: str | None = None,
    ) -> schema.ResourceContentBlock:
        return await asyncio.to_thread(
            self._publish_sync,
            session_id,
            run_id,
            virtual_path,
            outputs_path,
        )
