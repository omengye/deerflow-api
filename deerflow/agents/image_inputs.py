"""Safe, lightweight image attachments for model input.

Image bytes are stored in the thread uploads directory.  Checkpointed human
messages carry only :class:`InputImage` metadata; ``ViewImageMiddleware`` loads
and base64-encodes the files for the immediate model request.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from deerflow.uploads import ensure_uploads_dir, normalize_filename, upload_virtual_path

INPUT_IMAGES_KEY = "deerflow_input_images"
MAX_INPUT_IMAGE_BYTES = 20 * 1024 * 1024
MAX_INPUT_IMAGES_PER_TURN = 8
MAX_INPUT_IMAGE_TOTAL_BYTES = 40 * 1024 * 1024

IMAGE_EXTENSION_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
IMAGE_MIME_TO_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
}


@dataclass(frozen=True, slots=True)
class PendingInputImage:
    """Validated image bytes waiting to be persisted for a thread."""

    name: str
    mime_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class InputImage:
    """Serializable metadata for one persisted model-input image."""

    name: str
    mime_type: str
    virtual_path: str
    size: int
    sha256: str

    def to_metadata(self) -> dict[str, str | int]:
        return asdict(self)


def normalize_image_mime(mime_type: str | None) -> str | None:
    if not isinstance(mime_type, str):
        return None
    normalized = mime_type.split(";", 1)[0].strip().lower()
    return _MIME_ALIASES.get(normalized, normalized) or None


def detect_image_mime(image_data: bytes) -> str | None:
    """Detect the supported image MIME type from magic bytes."""

    if image_data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if (
        len(image_data) >= 12
        and image_data.startswith(b"RIFF")
        and image_data[8:12] == b"WEBP"
    ):
        return "image/webp"
    if image_data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def validate_image_bytes(
    image_data: bytes,
    *,
    declared_mime_type: str | None,
    max_bytes: int = MAX_INPUT_IMAGE_BYTES,
) -> str:
    """Validate size and content, returning the detected MIME type."""

    if not image_data:
        raise ValueError("Image data is empty")
    if len(image_data) > max_bytes:
        raise ValueError(
            f"Image is {len(image_data)} bytes; maximum supported size is {max_bytes} bytes"
        )
    detected = detect_image_mime(image_data)
    if detected is None:
        raise ValueError("Image contents do not match a supported JPG, PNG, WebP, or GIF image")
    declared = normalize_image_mime(declared_mime_type)
    if declared is not None and declared not in IMAGE_MIME_TO_EXTENSION:
        raise ValueError(f"Unsupported image MIME type: {declared_mime_type}")
    if declared is not None and declared != detected:
        raise ValueError(
            f"Image contents are {detected}, but the prompt declares {declared}"
        )
    return detected


def decode_base64_image(data: str, *, declared_mime_type: str | None) -> PendingInputImage:
    """Strictly decode an ACP image payload and validate its real format."""

    if not isinstance(data, str) or not data.strip():
        raise ValueError("Image data must be non-empty base64 text")
    encoded = data.strip()
    maximum_encoded_length = 4 * ((MAX_INPUT_IMAGE_BYTES + 2) // 3)
    if len(encoded) > maximum_encoded_length:
        raise ValueError(
            f"Encoded image exceeds the {MAX_INPUT_IMAGE_BYTES}-byte image limit"
        )
    try:
        image_data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Image data is not valid base64") from exc
    mime_type = validate_image_bytes(
        image_data,
        declared_mime_type=declared_mime_type,
    )
    return PendingInputImage(
        name=f"image{IMAGE_MIME_TO_EXTENSION[mime_type]}",
        mime_type=mime_type,
        data=image_data,
    )


def pending_image_from_file(
    path: Path,
    *,
    name: str,
    declared_mime_type: str | None,
) -> PendingInputImage:
    """Read and validate one already size-gated local image file."""

    size = path.stat().st_size
    if size > MAX_INPUT_IMAGE_BYTES:
        raise ValueError(
            f"Image is {size} bytes; maximum supported size is {MAX_INPUT_IMAGE_BYTES} bytes"
        )
    image_data = path.read_bytes()
    mime_type = validate_image_bytes(
        image_data,
        declared_mime_type=declared_mime_type,
    )
    try:
        safe_name = normalize_filename(name)
    except ValueError:
        safe_name = f"image{IMAGE_MIME_TO_EXTENSION[mime_type]}"
    return PendingInputImage(name=safe_name, mime_type=mime_type, data=image_data)


def validate_image_turn(images: Sequence[PendingInputImage]) -> None:
    if len(images) > MAX_INPUT_IMAGES_PER_TURN:
        raise ValueError(
            f"A prompt may include at most {MAX_INPUT_IMAGES_PER_TURN} images"
        )
    total_bytes = sum(len(image.data) for image in images)
    if total_bytes > MAX_INPUT_IMAGE_TOTAL_BYTES:
        raise ValueError(
            "Prompt images total "
            f"{total_bytes} bytes; maximum is {MAX_INPUT_IMAGE_TOTAL_BYTES} bytes"
        )


def persist_input_images(
    thread_id: str,
    images: Sequence[PendingInputImage],
) -> list[InputImage]:
    """Atomically persist validated images and return checkpoint-safe metadata."""

    validate_image_turn(images)
    if not images:
        return []
    uploads_dir = ensure_uploads_dir(thread_id)
    persisted_paths: list[Path] = []
    result: list[InputImage] = []
    try:
        for image in images:
            extension = IMAGE_MIME_TO_EXTENSION[image.mime_type]
            stored_name = f"acp-image-{uuid.uuid4().hex}{extension}"
            target = uploads_dir / stored_name
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=".acp-image-",
                    suffix=".tmp",
                    dir=uploads_dir,
                    delete=False,
                ) as temporary:
                    temporary.write(image.data)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_path = Path(temporary.name)
                os.replace(temporary_path, target)
                temporary_path = None
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            persisted_paths.append(target)
            result.append(
                InputImage(
                    name=image.name,
                    mime_type=image.mime_type,
                    virtual_path=upload_virtual_path(stored_name),
                    size=len(image.data),
                    sha256=hashlib.sha256(image.data).hexdigest(),
                )
            )
    except BaseException:
        for path in persisted_paths:
            path.unlink(missing_ok=True)
        raise
    return result


def normalize_input_image_metadata(value: Any) -> list[dict[str, str | int]]:
    """Return only well-formed, JSON-safe image metadata entries."""

    if not isinstance(value, list):
        return []
    normalized: list[dict[str, str | int]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        mime_type = normalize_image_mime(item.get("mime_type"))
        virtual_path = item.get("virtual_path")
        size = item.get("size")
        sha256 = item.get("sha256")
        if (
            not isinstance(name, str)
            or mime_type not in IMAGE_MIME_TO_EXTENSION
            or not isinstance(virtual_path, str)
            or not virtual_path.startswith("/mnt/user-data/")
            or not isinstance(size, int)
            or size < 1
            or size > MAX_INPUT_IMAGE_BYTES
            or not isinstance(sha256, str)
            or len(sha256) != 64
        ):
            continue
        normalized.append(
            {
                "name": name,
                "mime_type": mime_type,
                "virtual_path": virtual_path,
                "size": size,
                "sha256": sha256,
            }
        )
    return normalized[:MAX_INPUT_IMAGES_PER_TURN]
