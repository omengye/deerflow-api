"""Download trusted ACP resource links into a thread's ACP workspace."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import httpx
from acp import schema

_SAFE_FILENAME = re.compile(r"[^\w. -]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class DownloadedACPArtifact:
    name: str
    virtual_path: str
    size: int
    sha256: str
    mime_type: str | None


def _safe_filename(value: str) -> str:
    if not value or "\\" in value:
        raise ValueError("ACP artifact filename is empty or unsafe")
    name = Path(value).name
    if name in {"", ".", ".."}:
        raise ValueError("ACP artifact filename is unsafe")
    name = _SAFE_FILENAME.sub("_", name).strip(" .")
    if not name:
        name = "artifact"
    if len(name.encode("utf-8")) > 255:
        suffix = Path(name).suffix[:32]
        name = name.encode("utf-8")[: 220 - len(suffix.encode("utf-8"))].decode(
            "utf-8", errors="ignore"
        ) + suffix
    return name


def _expected_sha256(resource: schema.ResourceContentBlock) -> str | None:
    metadata = resource.field_meta
    if not isinstance(metadata, dict):
        return None
    deerflow = metadata.get("deerflow")
    if not isinstance(deerflow, dict):
        return None
    value = deerflow.get("sha256")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return None
    return value.lower()


class ACPArtifactDownloader:
    def __init__(
        self,
        work_dir: Path,
        invocation_id: str,
        *,
        allowed_hosts: list[str],
        allow_insecure_http: bool = False,
        max_bytes: int,
        timeout_seconds: float,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.work_dir = work_dir.resolve()
        self.invocation_id = invocation_id
        self.destination = (self.work_dir / invocation_id).resolve()
        self.destination.relative_to(self.work_dir)
        self.allowed_hosts = {host.strip().lower() for host in allowed_hosts if host.strip()}
        self.allow_insecure_http = allow_insecure_http
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client
        self._claimed: set[str] = set()

    def _claim_name(self, raw_name: str) -> str:
        name = _safe_filename(raw_name)
        if name not in self._claimed:
            self._claimed.add(name)
            return name
        stem, suffix = Path(name).stem, Path(name).suffix
        index = 1
        while f"{stem}_{index}{suffix}" in self._claimed:
            index += 1
        name = f"{stem}_{index}{suffix}"
        self._claimed.add(name)
        return name

    def _result(
        self,
        resource: schema.ResourceContentBlock,
        target: Path,
        *,
        size: int,
        sha256: str,
    ) -> DownloadedACPArtifact:
        relative = target.resolve().relative_to(self.work_dir).as_posix()
        return DownloadedACPArtifact(
            name=target.name,
            virtual_path=f"/mnt/acp-workspace/{relative}",
            size=size,
            sha256=sha256,
            mime_type=resource.mime_type,
        )

    async def download(
        self,
        resource: schema.ResourceContentBlock,
    ) -> DownloadedACPArtifact:
        parsed = urlparse(resource.uri)
        if parsed.scheme == "file":
            return self._use_local_file(resource, parsed)
        if parsed.scheme == "http":
            if not self.allow_insecure_http:
                raise ValueError(
                    "ACP artifact HTTP links require artifact_allow_insecure_http: true"
                )
        elif parsed.scheme != "https":
            raise ValueError("ACP artifact links must use HTTPS or explicitly allowed HTTP")
        if parsed.netloc.lower() not in self.allowed_hosts:
            raise ValueError(f"ACP artifact host is not allowed: {parsed.netloc}")
        if resource.size is not None and resource.size > self.max_bytes:
            raise ValueError(
                f"ACP artifact declares {resource.size} bytes; maximum is {self.max_bytes}"
            )

        name = self._claim_name(resource.name)
        self.destination.mkdir(parents=True, exist_ok=True)
        target = (self.destination / name).resolve()
        target.relative_to(self.destination)
        temporary = target.with_name(f".{target.name}.part")
        digest = hashlib.sha256()
        written = 0
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        try:
            async with client.stream("GET", resource.uri) as response:
                if response.status_code != 200:
                    raise ValueError(
                        f"ACP artifact download returned HTTP {response.status_code}"
                    )
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared = int(content_length)
                    except ValueError as exc:
                        raise ValueError("ACP artifact has an invalid Content-Length") from exc
                    if declared > self.max_bytes:
                        raise ValueError(
                            f"ACP artifact response is {declared} bytes; maximum is {self.max_bytes}"
                        )
                with temporary.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > self.max_bytes:
                            raise ValueError(
                                f"ACP artifact exceeds maximum size of {self.max_bytes} bytes"
                            )
                        digest.update(chunk)
                        output.write(chunk)
            if resource.size is not None and written != resource.size:
                raise ValueError(
                    f"ACP artifact size mismatch: expected {resource.size}, received {written}"
                )
            actual_sha256 = digest.hexdigest()
            expected_sha256 = _expected_sha256(resource)
            if expected_sha256 is not None and actual_sha256 != expected_sha256:
                raise ValueError(
                    f"ACP artifact checksum mismatch: expected {expected_sha256}, received {actual_sha256}"
                )
            temporary.replace(target)
            return self._result(
                resource,
                target,
                size=written,
                sha256=actual_sha256,
            )
        finally:
            temporary.unlink(missing_ok=True)
            if owns_client:
                await client.aclose()

    def _use_local_file(
        self,
        resource: schema.ResourceContentBlock,
        parsed,
    ) -> DownloadedACPArtifact:
        raw_path = url2pathname(unquote(parsed.path))
        if parsed.netloc and parsed.netloc not in {"", "localhost"}:
            raw_path = f"//{parsed.netloc}{raw_path}"
        if re.match(r"^/[A-Za-z]:/", raw_path):
            raw_path = raw_path[1:]
        source = Path(raw_path).resolve()
        source.relative_to(self.work_dir)
        if not source.is_file():
            raise ValueError(f"Local ACP artifact does not exist: {resource.uri}")
        size = source.stat().st_size
        if size > self.max_bytes:
            raise ValueError(f"Local ACP artifact exceeds maximum size of {self.max_bytes} bytes")
        return self._result(
            resource,
            source,
            size=size,
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        )
