"""Qiniu Kodo object storage tools."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, NamedTuple
from urllib.parse import quote

import httpx
from langchain.tools import ToolRuntime, tool

from deerflow.agents.thread_state import AgentContext, ThreadDataState, ThreadState
from deerflow.community.proxy import get_tool_https_proxy
from deerflow.config import get_app_config
from deerflow.config.paths import VIRTUAL_PATH_PREFIX

logger = logging.getLogger(__name__)

_DEFAULT_UPLOAD_TOKEN_EXPIRES = 3600
_DEFAULT_DOWNLOAD_URL_EXPIRES = 3600
_DEFAULT_DOWNLOAD_TIMEOUT = 60
_MAX_DOWNLOAD_TIMEOUT = 600
_DEFAULT_MAX_DOWNLOAD_SIZE_MB = 200
_MAX_LIST_LIMIT = 1000
_QINIU_TOOL_NAMES = {
    "qiniu_upload_file",
    "qiniu_download_file",
    "qiniu_list_objects",
    "qiniu_stat_object",
    "qiniu_delete_object",
    "qiniu_get_download_url",
}


class _QiniuSdk(NamedTuple):
    Auth: type
    BucketManager: type
    put_file: Callable[..., tuple[Any, Any]]


@dataclass(frozen=True)
class _QiniuConfig:
    access_key: str
    secret_key: str
    bucket: str
    domain: str | None = None
    key_prefix: str = "deerflow/{thread_id}/"
    private_bucket: bool = False
    upload_token_expires: int = _DEFAULT_UPLOAD_TOKEN_EXPIRES
    download_url_expires: int = _DEFAULT_DOWNLOAD_URL_EXPIRES
    download_timeout: int = _DEFAULT_DOWNLOAD_TIMEOUT
    max_download_size_mb: int = _DEFAULT_MAX_DOWNLOAD_SIZE_MB


def _json_output(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _json_error(message: str, **extra: Any) -> str:
    payload = {"error": message}
    payload.update(extra)
    return _json_output(payload)


def _load_qiniu_sdk() -> _QiniuSdk:
    try:
        import qiniu  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError("qiniu package is not installed. Install the optional dependency: deerflow-api[qiniu]") from exc

    put_file = getattr(qiniu, "put_file_v2", None) or getattr(qiniu, "put_file", None)
    if put_file is None:
        raise RuntimeError("Installed qiniu package does not expose put_file or put_file_v2")

    return _QiniuSdk(
        Auth=qiniu.Auth,
        BucketManager=qiniu.BucketManager,
        put_file=put_file,
    )


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return _as_bool(raw, default)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: Any, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


def _tool_extra(tool_name: str) -> dict[str, Any]:
    try:
        config = get_app_config()
    except Exception:
        return {}

    merged: dict[str, Any] = {}
    # Allow one configured Qiniu tool to carry shared credentials for all Qiniu
    # tools. Per-tool values below override this shared baseline.
    for configured_tool in config.tools:
        if configured_tool.name in _QINIU_TOOL_NAMES and configured_tool.model_extra:
            for key, value in configured_tool.model_extra.items():
                merged.setdefault(key, value)

    current = config.get_tool_config(tool_name)
    if current is not None and current.model_extra:
        merged.update(current.model_extra)
    return merged


def _get_str(extra: dict[str, Any], key: str, env_name: str, default: str | None = None) -> str | None:
    value = extra.get(key)
    if value is None or value == "":
        value = os.getenv(env_name, default)
    if value is None:
        return None
    return str(value)


def _resolve_config(tool_name: str) -> _QiniuConfig:
    extra = _tool_extra(tool_name)
    access_key = _get_str(extra, "access_key", "QINIU_ACCESS_KEY")
    secret_key = _get_str(extra, "secret_key", "QINIU_SECRET_KEY")
    bucket = _get_str(extra, "bucket", "QINIU_BUCKET")

    missing = [
        name
        for name, value in (
            ("access_key", access_key),
            ("secret_key", secret_key),
            ("bucket", bucket),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "Qiniu Kodo is not configured. Set "
            + ", ".join(missing)
            + f" in config.yaml under {tool_name} or via QINIU_ACCESS_KEY/QINIU_SECRET_KEY/QINIU_BUCKET."
        )

    domain = _get_str(extra, "domain", "QINIU_DOMAIN")
    key_prefix = _get_str(extra, "key_prefix", "QINIU_KEY_PREFIX", "deerflow/{thread_id}/") or ""
    private_bucket = _as_bool(extra.get("private_bucket"), _env_bool("QINIU_PRIVATE_BUCKET", False))

    return _QiniuConfig(
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        domain=domain,
        key_prefix=key_prefix,
        private_bucket=private_bucket,
        upload_token_expires=_as_int(extra.get("upload_token_expires"), _DEFAULT_UPLOAD_TOKEN_EXPIRES),
        download_url_expires=_as_int(extra.get("download_url_expires"), _DEFAULT_DOWNLOAD_URL_EXPIRES),
        download_timeout=_as_int(extra.get("download_timeout"), _DEFAULT_DOWNLOAD_TIMEOUT, maximum=_MAX_DOWNLOAD_TIMEOUT),
        max_download_size_mb=_as_int(extra.get("max_download_size_mb"), _DEFAULT_MAX_DOWNLOAD_SIZE_MB),
    )


def _qiniu_auth(config: _QiniuConfig, sdk: _QiniuSdk) -> Any:
    return sdk.Auth(config.access_key, config.secret_key)


def _bucket_manager(config: _QiniuConfig, sdk: _QiniuSdk) -> Any:
    return sdk.BucketManager(_qiniu_auth(config, sdk))


def _get_thread_id(runtime: ToolRuntime[AgentContext, ThreadState] | None) -> str | None:
    context = getattr(runtime, "context", None) or {}
    thread_id = context.get("thread_id")
    if thread_id:
        return str(thread_id)

    runtime_config = getattr(runtime, "config", None) or {}
    thread_id = runtime_config.get("configurable", {}).get("thread_id")
    if thread_id:
        return str(thread_id)

    state = getattr(runtime, "state", None) or {}
    thread_data = state.get("thread_data") or {}
    workspace_path = thread_data.get("workspace_path")
    if workspace_path:
        try:
            return Path(workspace_path).parent.parent.name
        except Exception:
            pass

    try:
        from langgraph.config import get_config

        thread_id = get_config().get("configurable", {}).get("thread_id")
        return str(thread_id) if thread_id else None
    except RuntimeError:
        return None


def _get_thread_data(runtime: ToolRuntime[AgentContext, ThreadState] | None) -> ThreadDataState | None:
    from deerflow.sandbox.tools import get_thread_data

    return get_thread_data(runtime)


def _is_user_data_virtual_path(path: str) -> bool:
    return path == VIRTUAL_PATH_PREFIX or path.startswith(f"{VIRTUAL_PATH_PREFIX}/")


def _is_writable_virtual_path(path: str) -> bool:
    allowed_roots = (
        f"{VIRTUAL_PATH_PREFIX}/workspace",
        f"{VIRTUAL_PATH_PREFIX}/outputs",
    )
    return any(path == root or path.startswith(f"{root}/") for root in allowed_roots)


def _resolve_readable_file(runtime: ToolRuntime[AgentContext, ThreadState], virtual_path: str) -> Path:
    from deerflow.sandbox.exceptions import SandboxRuntimeError
    from deerflow.sandbox.tools import resolve_and_validate_user_data_path, validate_local_tool_path

    if not _is_user_data_virtual_path(virtual_path):
        raise PermissionError(f"Only files under {VIRTUAL_PATH_PREFIX}/ can be uploaded")

    thread_data = _get_thread_data(runtime)
    if thread_data is None:
        raise SandboxRuntimeError("Thread data not available for Qiniu upload")

    validate_local_tool_path(virtual_path, thread_data, read_only=True)
    local_path = Path(resolve_and_validate_user_data_path(virtual_path, thread_data))
    if not local_path.exists():
        raise FileNotFoundError(f"File not found: {virtual_path}")
    if not local_path.is_file():
        raise ValueError(f"Path is not a file: {virtual_path}")
    return local_path


def _resolve_writable_path(runtime: ToolRuntime[AgentContext, ThreadState], virtual_path: str, key: str) -> Path:
    from deerflow.sandbox.exceptions import SandboxRuntimeError
    from deerflow.sandbox.tools import resolve_and_validate_user_data_path, validate_local_tool_path

    if not _is_writable_virtual_path(virtual_path):
        raise PermissionError(f"Downloaded files must be saved under {VIRTUAL_PATH_PREFIX}/workspace or {VIRTUAL_PATH_PREFIX}/outputs")

    thread_data = _get_thread_data(runtime)
    if thread_data is None:
        raise SandboxRuntimeError("Thread data not available for Qiniu download")

    target_path = virtual_path
    if target_path.endswith("/"):
        target_path = f"{target_path}{_object_basename(key)}"

    validate_local_tool_path(target_path, thread_data, read_only=False)
    return Path(resolve_and_validate_user_data_path(target_path, thread_data))


def _normalize_key_path(value: str, *, allow_empty: bool = False) -> str:
    raw = value.replace("\\", "/").strip().lstrip("/")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ValueError("Object key must not contain '..' path segments")
    normalized = "/".join(parts)
    if not normalized and not allow_empty:
        raise ValueError("Object key is required")
    return normalized


def _render_key_prefix(config: _QiniuConfig, runtime: ToolRuntime[AgentContext, ThreadState] | None) -> str:
    prefix = config.key_prefix or ""
    if "{thread_id}" in prefix:
        thread_id = _get_thread_id(runtime)
        if not thread_id:
            raise ValueError("Thread ID is required to render Qiniu key_prefix")
        prefix = prefix.replace("{thread_id}", thread_id)
    normalized = _normalize_key_path(prefix, allow_empty=True)
    return f"{normalized}/" if normalized else ""


def _prefix_key(config: _QiniuConfig, runtime: ToolRuntime[AgentContext, ThreadState] | None, key: str) -> str:
    normalized_key = _normalize_key_path(key)
    prefix = _render_key_prefix(config, runtime)
    if prefix and normalized_key.startswith(prefix):
        return normalized_key
    return f"{prefix}{normalized_key}"


def _prefix_list_prefix(config: _QiniuConfig, runtime: ToolRuntime[AgentContext, ThreadState] | None, prefix: str) -> str:
    configured_prefix = _render_key_prefix(config, runtime)
    requested = _normalize_key_path(prefix, allow_empty=True)
    if configured_prefix and requested.startswith(configured_prefix):
        return requested
    return f"{configured_prefix}{requested}" if requested else configured_prefix


def _default_key_for_path(virtual_path: str) -> str:
    stripped = virtual_path.lstrip("/")
    virtual_root = VIRTUAL_PATH_PREFIX.lstrip("/")
    if stripped == virtual_root:
        raise ValueError(f"Cannot upload {VIRTUAL_PATH_PREFIX} root as a file")
    if not stripped.startswith(f"{virtual_root}/"):
        raise ValueError(f"Path must start with {VIRTUAL_PATH_PREFIX}/")
    return _normalize_key_path(stripped[len(virtual_root) :].lstrip("/"))


def _object_basename(key: str) -> str:
    name = PurePosixPath(key).name
    if not name:
        raise ValueError("Object key does not contain a filename")
    return name


def _guess_mime_type(path: Path, configured_mime_type: str | None) -> str | None:
    if configured_mime_type:
        return configured_mime_type
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed


def _info_ok(info: Any) -> bool:
    if info is None:
        return True
    ok = getattr(info, "ok", None)
    if callable(ok):
        return bool(ok())
    if ok is not None:
        return bool(ok)
    status_code = getattr(info, "status_code", None)
    if status_code is None:
        return False
    try:
        return 200 <= int(status_code) < 300
    except (TypeError, ValueError):
        return False


def _info_payload(info: Any) -> dict[str, Any]:
    if info is None:
        return {}
    payload: dict[str, Any] = {}
    for attr in ("status_code", "req_id", "x_log", "error"):
        value = getattr(info, attr, None)
        if value is not None:
            payload[attr] = value
    text_body = getattr(info, "text_body", None)
    if text_body and "error" not in payload:
        payload["error"] = text_body
    return payload


def _ensure_qiniu_success(info: Any, action: str) -> None:
    if _info_ok(info):
        return
    detail = _info_payload(info)
    message = detail.get("error") or f"Qiniu {action} failed"
    raise RuntimeError(str(message))


def _upload_file(
    sdk: _QiniuSdk,
    auth: Any,
    config: _QiniuConfig,
    local_path: Path,
    key: str,
    overwrite: bool,
    mime_type: str | None,
) -> tuple[Any, Any]:
    policy = None if overwrite else {"insertOnly": 1}
    upload_token = auth.upload_token(
        config.bucket,
        key,
        config.upload_token_expires,
        policy=policy,
    )
    actual_mime_type = _guess_mime_type(local_path, mime_type)
    try:
        if actual_mime_type:
            return sdk.put_file(upload_token, key, str(local_path), mime_type=actual_mime_type)
        return sdk.put_file(upload_token, key, str(local_path))
    except TypeError:
        return sdk.put_file(upload_token, key, str(local_path))


def _object_url(config: _QiniuConfig, auth: Any, key: str, expires: int | None = None) -> str:
    if not config.domain:
        raise ValueError("Qiniu domain is not configured. Set domain in config.yaml or QINIU_DOMAIN.")
    domain = config.domain.strip().rstrip("/")
    if not domain.startswith(("http://", "https://")):
        domain = f"https://{domain}"
    public_url = f"{domain}/{quote(key, safe='/-_.~')}"
    if not config.private_bucket:
        return public_url
    return auth.private_download_url(public_url, expires=expires or config.download_url_expires)


def _download_to_path(url: str, destination: Path, *, max_bytes: int, timeout: int, https_proxy: str | None) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with httpx.Client(timeout=timeout, proxy=https_proxy or None, follow_redirects=True, trust_env=False) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with open(destination, "wb") as out:
                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > max_bytes:
                            raise RuntimeError(f"Download exceeds configured size limit of {max_bytes} bytes")
                        out.write(chunk)
    except Exception:
        if destination.exists():
            destination.unlink(missing_ok=True)
        raise
    return written


@tool("qiniu_upload_file", parse_docstring=True)
def qiniu_upload_file_tool(
    runtime: ToolRuntime[AgentContext, ThreadState],
    path: str,
    key: str | None = None,
    overwrite: bool = False,
    mime_type: str | None = None,
) -> str:
    """Upload a local thread file to Qiniu Kodo object storage.

    Args:
        path: Absolute /mnt/user-data virtual path to the file to upload.
        key: Optional object key or key suffix. If omitted, the key is derived from the virtual path.
        overwrite: Whether to allow replacing an existing object with the same key.
        mime_type: Optional content MIME type. If omitted, it is guessed from the file extension.
    """
    try:
        config = _resolve_config("qiniu_upload_file")
        local_path = _resolve_readable_file(runtime, path)
        object_key = _prefix_key(config, runtime, key or _default_key_for_path(path))
        sdk = _load_qiniu_sdk()
        auth = _qiniu_auth(config, sdk)
        ret, info = _upload_file(sdk, auth, config, local_path, object_key, overwrite, mime_type)
        _ensure_qiniu_success(info, "upload")
        url = _object_url(config, auth, object_key) if config.domain else None
        payload = {
            "bucket": config.bucket,
            "key": object_key,
            "size": local_path.stat().st_size,
            "hash": ret.get("hash") if isinstance(ret, dict) else None,
            "url": url,
            "response": ret if isinstance(ret, dict) else {},
        }
        return _json_output(payload)
    except Exception as exc:
        logger.error("Qiniu upload failed: %s", type(exc).__name__)
        return _json_error(str(exc), path=path)


@tool("qiniu_download_file", parse_docstring=True)
def qiniu_download_file_tool(
    runtime: ToolRuntime[AgentContext, ThreadState],
    key: str,
    destination_path: str,
    expires: int = _DEFAULT_DOWNLOAD_URL_EXPIRES,
) -> str:
    """Download a Qiniu Kodo object into the current thread workspace or outputs directory.

    Args:
        key: Object key or key suffix to download.
        destination_path: Absolute /mnt/user-data/workspace or /mnt/user-data/outputs virtual path for the downloaded file. If it ends with '/', the object filename is used.
        expires: Private bucket signed URL lifetime in seconds.
    """
    try:
        config = _resolve_config("qiniu_download_file")
        object_key = _prefix_key(config, runtime, key)
        target_path = _resolve_writable_path(runtime, destination_path, object_key)
        sdk = _load_qiniu_sdk()
        auth = _qiniu_auth(config, sdk)
        download_url = _object_url(config, auth, object_key, expires=_as_int(expires, config.download_url_expires))
        max_bytes = config.max_download_size_mb * 1024 * 1024
        size = _download_to_path(
            download_url,
            target_path,
            max_bytes=max_bytes,
            timeout=config.download_timeout,
            https_proxy=get_tool_https_proxy("qiniu_download_file"),
        )
        return _json_output(
            {
                "bucket": config.bucket,
                "key": object_key,
                "destination_path": destination_path,
                "size": size,
            }
        )
    except Exception as exc:
        logger.error("Qiniu download failed: %s", type(exc).__name__)
        return _json_error(str(exc), key=key)


@tool("qiniu_list_objects", parse_docstring=True)
def qiniu_list_objects_tool(
    runtime: ToolRuntime[AgentContext, ThreadState],
    prefix: str = "",
    limit: int = 100,
    marker: str | None = None,
) -> str:
    """List Qiniu Kodo objects under the configured prefix.

    Args:
        prefix: Optional object key prefix or prefix suffix to list.
        limit: Maximum number of objects to return.
        marker: Optional pagination marker from the previous list response.
    """
    try:
        config = _resolve_config("qiniu_list_objects")
        sdk = _load_qiniu_sdk()
        object_prefix = _prefix_list_prefix(config, runtime, prefix)
        manager = _bucket_manager(config, sdk)
        ret, eof, info = manager.list(
            config.bucket,
            prefix=object_prefix,
            marker=marker,
            limit=_as_int(limit, 100, maximum=_MAX_LIST_LIMIT),
        )
        _ensure_qiniu_success(info, "list")
        items = ret.get("items", []) if isinstance(ret, dict) else []
        return _json_output(
            {
                "bucket": config.bucket,
                "prefix": object_prefix,
                "marker": ret.get("marker") if isinstance(ret, dict) else None,
                "eof": bool(eof),
                "count": len(items),
                "items": items,
            }
        )
    except Exception as exc:
        logger.error("Qiniu list failed: %s", type(exc).__name__)
        return _json_error(str(exc), prefix=prefix)


@tool("qiniu_stat_object", parse_docstring=True)
def qiniu_stat_object_tool(
    runtime: ToolRuntime[AgentContext, ThreadState],
    key: str,
) -> str:
    """Get metadata for a Qiniu Kodo object.

    Args:
        key: Object key or key suffix to inspect.
    """
    try:
        config = _resolve_config("qiniu_stat_object")
        sdk = _load_qiniu_sdk()
        object_key = _prefix_key(config, runtime, key)
        ret, info = _bucket_manager(config, sdk).stat(config.bucket, object_key)
        _ensure_qiniu_success(info, "stat")
        return _json_output(
            {
                "bucket": config.bucket,
                "key": object_key,
                "metadata": ret if isinstance(ret, dict) else {},
                "info": _info_payload(info),
            }
        )
    except Exception as exc:
        logger.error("Qiniu stat failed: %s", type(exc).__name__)
        return _json_error(str(exc), key=key)


@tool("qiniu_delete_object", parse_docstring=True)
def qiniu_delete_object_tool(
    runtime: ToolRuntime[AgentContext, ThreadState],
    key: str,
) -> str:
    """Delete a Qiniu Kodo object.

    Args:
        key: Object key or key suffix to delete.
    """
    try:
        config = _resolve_config("qiniu_delete_object")
        sdk = _load_qiniu_sdk()
        object_key = _prefix_key(config, runtime, key)
        ret, info = _bucket_manager(config, sdk).delete(config.bucket, object_key)
        _ensure_qiniu_success(info, "delete")
        return _json_output(
            {
                "bucket": config.bucket,
                "key": object_key,
                "deleted": True,
                "response": ret if isinstance(ret, dict) else {},
                "info": _info_payload(info),
            }
        )
    except Exception as exc:
        logger.error("Qiniu delete failed: %s", type(exc).__name__)
        return _json_error(str(exc), key=key)


@tool("qiniu_get_download_url", parse_docstring=True)
def qiniu_get_download_url_tool(
    runtime: ToolRuntime[AgentContext, ThreadState],
    key: str,
    expires: int = _DEFAULT_DOWNLOAD_URL_EXPIRES,
) -> str:
    """Create a public or private download URL for a Qiniu Kodo object.

    Args:
        key: Object key or key suffix for the object.
        expires: Private bucket signed URL lifetime in seconds.
    """
    try:
        config = _resolve_config("qiniu_get_download_url")
        sdk = _load_qiniu_sdk()
        auth = _qiniu_auth(config, sdk)
        object_key = _prefix_key(config, runtime, key)
        url = _object_url(config, auth, object_key, expires=_as_int(expires, config.download_url_expires))
        return _json_output(
            {
                "bucket": config.bucket,
                "key": object_key,
                "url": url,
                "private_bucket": config.private_bucket,
                "expires": expires if config.private_bucket else None,
            }
        )
    except Exception as exc:
        logger.error("Qiniu URL generation failed: %s", type(exc).__name__)
        return _json_error(str(exc), key=key)
