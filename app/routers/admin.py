"""Admin-only configuration endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import string
import tempfile
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import FeishuSettings, ThreadCleanupSettings, settings
from app.dependencies import get_client_manager
from app.proposal_review import (
    approve_skill_proposal,
    get_skill_catalog_version,
    proposal_app_config_context,
    refresh_skill_prompt_cache,
    reject_skill_proposal,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.extensions_config import (
    ExtensionsConfig,
    get_extensions_config_lock,
    load_extensions_config_data,
    write_extensions_config_data,
)
from deerflow.config.memory_config import MemoryConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.subagents_config import SubagentsAppConfig
from deerflow.config.summarization_config import ContextSize, SummarizationConfig
from deerflow.config.title_config import TitleConfig
from deerflow.runtime.scheduler import ScheduledTask, SchedulerStore
from deerflow.skills.evolution import (
    SkillEvolutionService,
    SkillPublishConflict,
    get_evolution_store,
)
from deerflow.skills.evolution.worker import get_evolution_worker
from deerflow.skills.manager import (
    ALLOWED_SUPPORT_SUBDIRS,
    append_history,
    custom_skill_exists,
    ensure_custom_skill_is_editable,
    ensure_safe_support_path,
    get_custom_skill_dir,
    get_custom_skill_file,
    list_custom_skills,
    public_skill_exists,
    read_custom_skill_content,
    read_history,
    validate_skill_name,
)
from deerflow.subagents.builtins import BUILTIN_SUBAGENTS

logger = logging.getLogger(__name__)


def require_admin_api_enabled() -> None:
    """Disable Admin API whenever bearer auth is not configured."""
    if not settings.auth_enabled:
        raise HTTPException(status_code=404, detail="Admin API is disabled")


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_api_enabled)],
)

_SECRET_KEY_NAMES = {
    "api_key",
    "access_key",
    "secret_key",
    "secret",
    "token",
    "password",
    "authorization",
    "client_secret",
    "refresh_token",
    "app_secret",
}
_SECRET_KEY_PARTS = ("api_key", "secret", "password", "authorization")
_SAFE_REFERENCE_KEY_NAMES = {"api_key_env"}
_MEMORY_CANONICAL_KEYS = {
    "enabled",
    "manager_class",
    "mode",
    "injection_enabled",
    "shutdown_flush_timeout_seconds",
    "backend_config",
}
_MEMORY_PROBE_TIMEOUT_SECONDS = 130.0
_CONFIG_WRITE_LOCK = threading.RLock()


class AdminModelsUpdateRequest(BaseModel):
    models: list[dict[str, Any]] = Field(min_length=1)
    default_model: str | None = None
    reload: bool = True


class AdminModelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: dict[str, Any]
    set_default: bool = False
    reload: bool = True


class AdminModelPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: dict[str, Any] = Field(default_factory=dict)
    clear_api_key: bool = False
    set_default: bool = False
    reload: bool = True


class AdminReloadRequest(BaseModel):
    include_extensions: bool = True
    reset_clients: bool = True


class AdminFeishuUpdateRequest(BaseModel):
    enabled: bool = False
    app_id: str | None = None
    app_secret: Any = None
    verification_token: Any = None
    restart: bool = True


class AdminSkillUpsertRequest(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)
    enabled: bool | None = None
    reload: bool = True


class AdminSupportFileWriteRequest(BaseModel):
    content: str = Field(max_length=200_000)
    reload: bool = False


class AdminProposalReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_base_sha256: str | None = None
    note: str | None = Field(default=None, max_length=2000)


class AdminProposalArchiveBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_ids: list[str] = Field(min_length=1, max_length=2000)


class AdminSkillRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)


class AdminRuntimePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_name: str | None = None
    thinking_enabled: bool | None = None
    subagent_enabled: bool | None = None
    plan_mode: bool | None = None
    max_concurrent_subagents: int | None = Field(default=None, ge=2, le=4)
    chat_request_timeout: float | None = Field(default=None, gt=0, le=86_400)
    max_upload_size_mb: int | None = Field(default=None, ge=1, le=2048)
    max_uploads_per_request: int | None = Field(default=None, ge=1, le=200)
    allowed_upload_extensions: list[str] | None = None
    scheduler_enabled: bool | None = None
    scheduler_poll_interval_seconds: float | None = Field(default=None, ge=0.5, le=3600)
    scheduler_timezone: str | None = Field(default=None, min_length=1, max_length=128)
    scheduler_max_concurrent_runs: int | None = Field(default=None, ge=1, le=100)
    scheduler_max_attempts: int | None = Field(default=None, ge=1, le=20)
    scheduler_retry_base_seconds: float | None = Field(default=None, ge=0, le=3600)
    scheduler_claim_lease_seconds: float | None = Field(default=None, ge=5, le=86400)
    scheduler_shutdown_grace_seconds: float | None = Field(default=None, ge=0, le=600)
    scheduler_run_retention_days: int | None = Field(default=None, ge=1, le=3650)
    scheduler_max_runs_per_task: int | None = Field(default=None, ge=1, le=100000)
    reload: bool = True


class AdminThreadCleanupUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: ThreadCleanupSettings


class AdminThreadCleanupRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = False
    limit: int | None = Field(default=None, ge=1, le=2000)


class AdminMcpTestRequest(BaseModel):
    timeout_seconds: float = Field(default=5.0, gt=0, le=30)


class AdminTitleUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: TitleConfig
    reload: bool = True


class AdminSubagentsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: SubagentsAppConfig
    reload: bool = True


class AdminMemoryUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Stored secret values are restored before validating the candidate with
    # ``MemoryConfig``.  Typing this field as MemoryConfig would reject the
    # redaction sentinels returned by the Admin API before they can be restored.
    config: dict[str, Any]
    reload: bool = True


class AdminMemoryPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: str = Field(min_length=1, max_length=128)
    changes: dict[str, Any] = Field(default_factory=dict)
    backend_config_mode: str = Field(default="merge", pattern="^(merge|replace)$")
    probe: bool = True
    reload: bool = True


class AdminMemoryCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: dict[str, Any]


class AdminSummarizationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: SummarizationConfig
    reload: bool = True


_HOT_RUNTIME_FIELDS = {
    "model_name",
    "thinking_enabled",
    "subagent_enabled",
    "plan_mode",
    "max_concurrent_subagents",
    "chat_request_timeout",
    "max_upload_size_mb",
    "max_uploads_per_request",
    "allowed_upload_extensions",
}
_RESTART_RUNTIME_FIELDS = {
    "scheduler_enabled",
    "scheduler_poll_interval_seconds",
    "scheduler_timezone",
    "scheduler_max_concurrent_runs",
    "scheduler_max_attempts",
    "scheduler_retry_base_seconds",
    "scheduler_claim_lease_seconds",
    "scheduler_shutdown_grace_seconds",
    "scheduler_run_retention_days",
    "scheduler_max_runs_per_task",
}

_KNOWN_TOP_LEVEL_CONFIG_KEYS = {
    "config_version",
    "log_level",
    "api",
    "stream_bridge",
    "token_usage",
    "models",
    "default_model",
    "sandbox",
    "acp_agents",
    "agents_api",
    "skills",
    "skill_evolution",
    "memory",
    "subagents",
    "tool_groups",
    "tool_search",
    "tool_output",
    "tools",
    "guardrails",
    "title",
    "summarization",
    "tracing",
    "circuit_breaker",
    "llm_call",
    "checkpointer",
}


def _config_path() -> Path:
    path = Path(settings.config_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _admin_scheduler_store() -> tuple[SchedulerStore | None, Path, bool]:
    config_path = _config_path()
    config_data = _load_config_data(config_path)
    api_config = config_data.get("api") if isinstance(config_data.get("api"), dict) else {}
    raw_db_path = api_config.get("scheduler_db_path") or settings.scheduler_db_path
    db_path = Path(str(raw_db_path))
    if not db_path.is_absolute():
        db_path = config_path.parent / db_path
    enabled = bool(api_config.get("scheduler_enabled", settings.scheduler_enabled))
    resolved = db_path.resolve()
    return (SchedulerStore(resolved) if resolved.exists() else None, resolved, enabled)


def _admin_scheduled_task_response(task: ScheduledTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "thread_id": task.thread_id,
        "prompt": task.prompt,
        "schedule_type": task.schedule_type,
        "schedule_expr": task.schedule_expr,
        "timezone": task.timezone,
        "enabled": task.enabled,
        "next_run_at": task.next_run_at,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "multitask_strategy": task.multitask_strategy,
    }


@contextmanager
def _admin_app_config_context():
    with proposal_app_config_context():
        yield


class _VersionedConfigData(dict[str, Any]):
    """Config mapping carrying the file revision it was read from."""

    def __init__(self, data: dict[str, Any], *, source_sha256: str) -> None:
        super().__init__(data)
        self.source_sha256 = source_sha256


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _config_file_sha256(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read config file: {exc}") from exc


@contextmanager
def _config_write_guard(path: Path):
    """Serialize writes and use a POSIX lock file for multi-worker Linux."""
    with _CONFIG_WRITE_LOCK:
        lock_file = None
        if os.name == "posix":
            try:
                import fcntl

                lock_path = path.with_name(f".{path.name}.lock")
                lock_file = lock_path.open("a+b")
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                if lock_file is not None:
                    lock_file.close()
                raise HTTPException(status_code=500, detail=f"Failed to lock config file: {exc}") from exc
        try:
            yield
        finally:
            if lock_file is not None:
                try:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except OSError:
                    logger.warning("Failed to release Admin config file lock", exc_info=True)
                finally:
                    lock_file.close()


def _load_config_data(path: Path | None = None) -> dict[str, Any]:
    config_path = path or _config_path()
    if not config_path.exists():
        raise HTTPException(status_code=404, detail=f"Config file not found: {config_path}")
    try:
        raw = config_path.read_bytes()
        data = yaml.safe_load(raw.decode("utf-8")) or {}
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Config file is not valid UTF-8: {exc}") from exc
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=500, detail=f"Config file is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read config file: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="Config file must contain a YAML object")
    return _VersionedConfigData(data, source_sha256=_sha256_bytes(raw))


def _resolve_extensions_config_path(*, create: bool = False) -> Path | None:
    config_path = _config_path()
    config_data = _load_config_data(config_path)
    api_config = config_data.get("api") if isinstance(config_data.get("api"), dict) else {}
    skills_config = config_data.get("skills") if isinstance(config_data.get("skills"), dict) else {}

    raw_path = api_config.get("extensions_config_path") or skills_config.get("extensions_file")
    if raw_path:
        path = Path(str(raw_path))
        if not path.is_absolute():
            path = config_path.parent / path
        return path.resolve()

    env_path = os.environ.get("DEER_FLOW_EXTENSIONS_CONFIG_PATH")
    if env_path:
        return Path(env_path).resolve()

    try:
        resolved = ExtensionsConfig.resolve_config_path()
        if resolved is not None:
            return resolved.resolve()
    except FileNotFoundError:
        pass

    if create:
        return (config_path.parent / "extensions_config.json").resolve()
    return None


def _load_extensions_data(*, create: bool = False) -> tuple[Path | None, dict[str, Any]]:
    with get_extensions_config_lock():
        path = _resolve_extensions_config_path(create=create)
        if path is None:
            return None, {"mcpServers": {}, "skills": {}}
        if not path.exists():
            if not create:
                return path, {"mcpServers": {}, "skills": {}}
            write_extensions_config_data(path, {"mcpServers": {}, "skills": {}})
        try:
            data = load_extensions_config_data(path)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        data.setdefault("mcpServers", {})
        data.setdefault("skills", {})
        if not isinstance(data["mcpServers"], dict):
            raise HTTPException(status_code=500, detail="extensions mcpServers must be an object")
        if not isinstance(data["skills"], dict):
            raise HTTPException(status_code=500, detail="extensions skills must be an object")
        return path, data


def _write_extensions_data(path: Path | None, data: dict[str, Any]) -> None:
    with get_extensions_config_lock():
        if path is None:
            path = _resolve_extensions_config_path(create=True)
        if path is None:
            raise HTTPException(status_code=500, detail="Unable to resolve extensions config path")
        try:
            write_extensions_config_data(path, data)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to write extensions config: {exc}") from exc


def _validate_config_data(data: dict[str, Any], *, source_path: Path) -> AppConfig:
    # ``AppConfig.from_file`` also updates process-wide singleton configs.  A
    # pre-write validation must be side-effect free; otherwise a failed CAS or
    # disk write can leave runtime state ahead of the persisted file.
    raw = deepcopy(dict(data))
    AppConfig._check_config_version(raw, source_path)
    resolved = AppConfig.resolve_env_variables(raw)
    return AppConfig.model_validate(resolved)


def _atomic_write_config(data: dict[str, Any], *, path: Path) -> None:
    with _config_write_guard(path):
        expected_sha256 = getattr(data, "source_sha256", None)
        if expected_sha256 and _config_file_sha256(path) != expected_sha256:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "config_revision_conflict",
                    "message": "Config changed after it was loaded; refresh and retry.",
                },
            )

        try:
            _validate_config_data(data, source_path=path)
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            message = _safe_config_error_message(exc, data)
            raise HTTPException(status_code=400, detail=f"Updated config did not validate: {message}") from exc

        previous_mode = None
        try:
            previous_mode = path.stat().st_mode
        except OSError:
            pass

        tmp_path: Path | None = None
        replaced = False
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                suffix=".tmp",
                delete=False,
                dir=str(path.parent),
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                yaml.safe_dump(dict(data), tmp_file, allow_unicode=True, sort_keys=False)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            if expected_sha256 and _config_file_sha256(path) != expected_sha256:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "config_revision_conflict",
                        "message": "Config changed while the update was prepared; refresh and retry.",
                    },
                )
            if previous_mode is not None:
                os.chmod(tmp_path, previous_mode)
            tmp_path.replace(path)
            replaced = True
        except (OSError, yaml.YAMLError) as exc:
            raise HTTPException(status_code=500, detail=f"Failed to write config file: {exc}") from exc
        finally:
            if tmp_path is not None and not replaced:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to remove temporary Admin config file %s", tmp_path, exc_info=True)

        if os.name == "posix":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # The atomic replacement already committed.  Reporting the
                # request as failed here would invite a retry even though the
                # new config is visible, so retain success and log the weaker
                # crash-durability guarantee.
                logger.warning("Config was replaced but its parent directory could not be fsynced", exc_info=True)


def _is_secret_key(key: str | None) -> bool:
    if not key:
        return False
    normalized = key.lower().replace("-", "_")
    if normalized in _SAFE_REFERENCE_KEY_NAMES:
        return False
    if normalized in _SECRET_KEY_NAMES or any(part in normalized for part in _SECRET_KEY_PARTS):
        return True
    # Match credential fields such as access_token without treating token-count
    # settings such as max_tokens or max_input_tokens as secrets.
    return "token" in normalized.split("_")


def _redacted_secret(value: Any) -> Any:
    if value is None or value == "":
        return {"redacted": False, "configured": False}
    if isinstance(value, str) and value.strip().startswith(("$", "${")):
        return {"redacted": False, "configured": True, "source": "env_ref", "value": value}
    return {"redacted": True, "configured": True, "source": "literal"}


def _redact_value(key: str | None, value: Any) -> Any:
    normalized = (key or "").lower().replace("-", "_")
    if normalized in {"env", "headers"} and isinstance(value, dict):
        return {str(k): _redacted_secret(v) for k, v in value.items()}
    if _is_secret_key(key):
        return _redacted_secret(value)
    if isinstance(value, dict):
        return {str(k): _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(None, item) for item in value]
    return value


def _config_secret_values(value: Any, *, key: str | None = None, protected: bool = False) -> set[str]:
    """Collect configured credentials without exposing them to callers."""
    if isinstance(value, BaseModel):
        value = value.model_dump()

    normalized = (key or "").lower().replace("-", "_")
    protect_children = protected or normalized in {"env", "headers"}
    secrets: set[str] = set()
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            child_name = str(child_key)
            child_protected = protect_children or _is_secret_key(child_name)
            secrets.update(
                _config_secret_values(
                    child_value,
                    key=child_name,
                    protected=child_protected,
                )
            )
            if child_name.lower().replace("-", "_") == "api_key_env" and isinstance(child_value, str):
                env_value = os.environ.get(child_value)
                if env_value:
                    secrets.add(env_value)
        return secrets
    if isinstance(value, list):
        for item in value:
            secrets.update(_config_secret_values(item, key=key, protected=protect_children))
        return secrets
    if protect_children and value not in (None, ""):
        secrets.add(str(value))
        if isinstance(value, str) and value.startswith("$"):
            try:
                resolved = AppConfig.resolve_env_variables(value)
            except (TypeError, ValueError):
                resolved = value
            if resolved not in (None, "", value):
                secrets.add(str(resolved))
    return secrets


def _redact_config_error_text(message: str, *configs: Any) -> str:
    secrets: set[str] = set()
    for config in configs:
        secrets.update(_config_secret_values(config))
    for secret in sorted(secrets, key=len, reverse=True):
        message = message.replace(secret, "[REDACTED]")
    return message


def _safe_config_error_message(exc: Exception, *configs: Any) -> str:
    return _redact_config_error_text(str(exc) or type(exc).__name__, *configs)


def _safe_skill_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="Skill operation failed")


def _history_record(
    *,
    action: str,
    file_path: str,
    prev_content: str | None,
    new_content: str | None,
    scanner: dict[str, Any],
) -> dict[str, Any]:
    return {
        "action": action,
        "author": "admin",
        "file_path": file_path,
        "prev_content": prev_content,
        "new_content": new_content,
        "scanner": scanner,
    }


def _sanitize_history(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized = []
    for record in records:
        sanitized.append(
            {
                "ts": record.get("ts"),
                "action": record.get("action"),
                "author": record.get("author"),
                "thread_id": record.get("thread_id"),
                "file_path": record.get("file_path"),
                "scanner": record.get("scanner"),
            }
        )
    return sanitized


def _publication_scan_summary(result: dict[str, Any]) -> dict[str, str]:
    scans = result.get("scans") or []
    if not scans:
        return {"decision": "allow", "reason": "No candidate content required scanning."}
    decision = "warn" if any(scan.get("decision") == "warn" for scan in scans) else "allow"
    return {"decision": decision, "reason": " ".join(str(scan.get("reason") or "") for scan in scans).strip()}


def _list_custom_support_files(name: str) -> list[str]:
    skill_dir = get_custom_skill_dir(name)
    files: list[str] = []
    for subdir in sorted(ALLOWED_SUPPORT_SUBDIRS):
        root = skill_dir / subdir
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files.append(path.relative_to(skill_dir).as_posix())
    return files


def _list_revision_files(snapshot: Path | None) -> list[str]:
    if snapshot is None or not snapshot.exists():
        return []
    return sorted(path.relative_to(snapshot).as_posix() for path in snapshot.rglob("*") if path.is_file())


def _custom_skill_response(name: str, *, include_content: bool = False) -> dict[str, Any]:
    skill = next((s for s in list_custom_skills() if s.name == name), None)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Custom skill '{name}' not found")
    stat = skill.skill_file.stat() if skill.skill_file.exists() else None
    payload: dict[str, Any] = {
        "name": skill.name,
        "description": skill.description,
        "enabled": _skill_enabled_state(skill.name, bool(skill.enabled)),
        "path": str(skill.skill_file),
        "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat() if stat else None,
        "active_revision": get_evolution_store().get_active_revision(skill.name),
    }
    if include_content:
        payload["content"] = read_custom_skill_content(name)
        payload["files"] = _list_custom_support_files(name)
    return payload


def _set_skill_enabled(name: str, enabled: bool) -> None:
    with get_extensions_config_lock():
        path, data = _load_extensions_data(create=True)
        skills = data.setdefault("skills", {})
        raw_skill = skills.get(name)
        skill_data = dict(raw_skill) if isinstance(raw_skill, dict) else {}
        skill_data["enabled"] = enabled
        skills[name] = skill_data
        _write_extensions_data(path, data)
    try:
        get_evolution_store().bump_catalog(
            actor="admin",
            action="skill.enabled" if enabled else "skill.disabled",
            details={"skill_name": name},
        )
    except Exception:
        logger.exception("Failed to update Skill catalog version after Admin enabled-state change")


def _skill_enabled_state(name: str, default: bool) -> bool:
    _, data = _load_extensions_data(create=False)
    raw = data.get("skills", {}).get(name)
    if isinstance(raw, dict) and isinstance(raw.get("enabled"), bool):
        return bool(raw["enabled"])
    return default


async def _refresh_after_skill_change(*, reload: bool) -> dict[str, Any] | None:
    await refresh_skill_prompt_cache()
    if not reload:
        return None
    manager = get_client_manager()
    ext_path = _resolve_extensions_config_path(create=True)
    return manager.reload_runtime_config(
        include_extensions=True,
        reset_clients=True,
        extensions_config_path=str(ext_path) if ext_path else None,
    )


def _validate_support_path_or_raise(name: str, relative_path: str) -> Path:
    try:
        return ensure_safe_support_path(name, relative_path)
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


def _restore_redacted_values(incoming: Any, existing: Any, *, path: str = "") -> Any:
    if isinstance(incoming, dict):
        literal_sentinel = (
            set(incoming) == {"redacted", "configured", "source"}
            and incoming.get("redacted") is True
            and incoming.get("configured") is True
            and incoming.get("source") == "literal"
        )
        clear_sentinel = (
            set(incoming) == {"redacted", "configured"}
            and incoming.get("redacted") is False
            and incoming.get("configured") is False
        )
        env_sentinel = (
            set(incoming) == {"redacted", "configured", "source", "value"}
            and incoming.get("redacted") is False
            and incoming.get("configured") is True
            and incoming.get("source") == "env_ref"
        )
        if literal_sentinel:
            if existing is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot preserve redacted value for new field {path or '<root>'}",
                )
            return existing
        if clear_sentinel:
            return None
        if env_sentinel:
            return incoming["value"]
        if isinstance(existing, dict):
            return {
                str(k): _restore_redacted_values(v, existing.get(k), path=f"{path}.{k}" if path else str(k))
                for k, v in incoming.items()
            }
        return {
            str(k): _restore_redacted_values(v, None, path=f"{path}.{k}" if path else str(k))
            for k, v in incoming.items()
        }
    if isinstance(incoming, list):
        existing_items = existing if isinstance(existing, list) else []
        return [
            _restore_redacted_values(item, existing_items[index] if index < len(existing_items) else None, path=f"{path}[{index}]")
            for index, item in enumerate(incoming)
        ]
    return incoming


def _memory_raw_config(config_data: dict[str, Any]) -> dict[str, Any]:
    raw = config_data.get("memory")
    return deepcopy(raw) if isinstance(raw, dict) else {}


def _memory_revision(raw: dict[str, Any]) -> str:
    payload = json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _memory_canonical_dump(config: MemoryConfig) -> dict[str, Any]:
    """Return the v2 persisted/editable Memory shape without legacy duplicates."""
    return {
        "enabled": config.enabled,
        "manager_class": config.manager_class,
        "mode": config.mode,
        "injection_enabled": config.injection_enabled,
        "shutdown_flush_timeout_seconds": config.shutdown_flush_timeout_seconds,
        "backend_config": deepcopy(config.backend_config),
    }


def _deep_merge_mapping(existing: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(existing)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_mapping(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _validate_memory_candidate(config_data: dict[str, Any], raw_candidate: dict[str, Any]) -> MemoryConfig:
    try:
        config = MemoryConfig.model_validate(raw_candidate)
    except ValidationError as exc:
        errors = _validation_errors(exc)
        for error in errors:
            error["message"] = _redact_config_error_text(str(error["message"]), raw_candidate)
        raise HTTPException(status_code=400, detail={"code": "invalid_memory_config", "errors": errors}) from exc

    _validate_configured_model_name(config_data, config.model_name, field="memory.backend_config.model_name")
    try:
        from deerflow.agents.memory import validate_memory_manager_config

        validate_memory_manager_config(config)
    except Exception as exc:
        message = _safe_config_error_message(exc, config, raw_candidate)
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_memory_backend",
                "message": message,
            },
        ) from exc
    return config


def _probe_memory_candidate(config: MemoryConfig) -> dict[str, Any]:
    if not config.enabled:
        return {"ok": True, "skipped": True, "reason": "memory_disabled"}
    try:
        from deerflow.agents.memory import probe_memory_manager_config

        probe_memory_manager_config(config)
    except Exception as exc:
        message = _safe_config_error_message(exc, config)
        logger.info("Admin memory backend probe failed: %s", message)
        raise HTTPException(
            status_code=400,
            detail={
                "code": "memory_probe_failed",
                "message": message,
            },
        ) from exc
    return {"ok": True, "skipped": False}


async def _probe_memory_candidate_async(config: MemoryConfig) -> dict[str, Any]:
    """Probe without blocking the event loop and bound trusted custom code."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_probe_memory_candidate, config),
            timeout=_MEMORY_PROBE_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        logger.warning(
            "Admin memory backend probe exceeded %.1fs (%s)",
            _MEMORY_PROBE_TIMEOUT_SECONDS,
            config.manager_class,
        )
        raise HTTPException(
            status_code=504,
            detail={
                "code": "memory_probe_timeout",
                "message": f"Memory backend probe exceeded {_MEMORY_PROBE_TIMEOUT_SECONDS:.0f} seconds.",
            },
        ) from exc


def _memory_admin_response(config_data: dict[str, Any], *, reload_result: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = _memory_raw_config(config_data)
    config = MemoryConfig.model_validate(raw)
    canonical = _memory_canonical_dump(config)

    try:
        from deerflow.agents.memory.queue import get_memory_queue

        queue = get_memory_queue()
        runtime = {
            "pending_writes": queue.pending_count,
            "processing": queue.is_processing,
        }
    except Exception:
        logger.debug("Failed to read memory queue status", exc_info=True)
        runtime = {"pending_writes": None, "processing": None}

    credentials: dict[str, Any] | None = None
    if config.manager_class == "mem0":
        env_name = str(config.backend_config.get("api_key_env") or "")
        credentials = {
            "api_key_env": env_name,
            "env_present": bool(env_name and os.environ.get(env_name)),
        }

    return {
        "config": _redact_value("memory", config.model_dump()),
        "canonical_config": _redact_value("memory", canonical),
        "revision": _memory_revision(raw),
        "runtime": runtime,
        "credentials": credentials,
        "reload": reload_result,
    }


def _memory_capabilities_response() -> dict[str, Any]:
    from deerflow.agents.memory.backends.mem0.config import Mem0Config

    return {
        "modes": [
            {"id": "middleware", "label": "自动召回", "automatic_injection": True},
            {"id": "tool", "label": "按需工具召回", "automatic_injection": False},
        ],
        "backends": [
            {
                "id": "deermem",
                "label": "DeerMem（本地）",
                "supports_connection_test": True,
                "supports_fact_crud": True,
            },
            {
                "id": "mem0",
                "label": "mem0（HTTP）",
                "supports_connection_test": True,
                "supports_fact_crud": False,
            },
            {
                "id": "custom",
                "label": "自定义 Manager",
                "supports_connection_test": True,
                "supports_fact_crud": None,
            },
        ],
        "schemas": {
            "memory": MemoryConfig.model_json_schema(),
            "mem0": Mem0Config.model_json_schema(),
        },
    }


def _reload_memory_with_rollback(
    *,
    path: Path,
    previous_raw: dict[str, Any],
    previous_present: bool,
    candidate_raw: dict[str, Any],
    reload: bool,
) -> dict[str, Any] | None:
    if not reload:
        return None
    try:
        return _reload_after_config_write(reload=True)
    except Exception as exc:
        message = _safe_config_error_message(exc, candidate_raw, previous_raw)
        logger.error("Admin memory update wrote config but reload failed: %s", message)
        rollback_file = False
        rollback_runtime = False
        runtime_synchronized = False
        superseded_by_newer_config = False
        rollback_error: str | None = None
        try:
            rollback_data = _load_config_data(path)
            if _memory_revision(_memory_raw_config(rollback_data)) != _memory_revision(candidate_raw):
                # Another process saved a newer Memory revision after this
                # request committed.  Never clobber it while compensating for
                # this request's local reload failure; instead align this
                # worker with the now-current file.
                superseded_by_newer_config = True
                _reload_after_config_write(reload=True)
                runtime_synchronized = True
            else:
                if previous_present:
                    rollback_data["memory"] = previous_raw
                else:
                    rollback_data.pop("memory", None)
                _atomic_write_config(rollback_data, path=path)
                rollback_file = True
                _reload_after_config_write(reload=True)
                rollback_runtime = True
                runtime_synchronized = True
        except Exception as rollback_exc:
            rollback_error = _safe_config_error_message(rollback_exc, candidate_raw, previous_raw)
            logger.error("Admin memory update rollback failed: %s", rollback_error)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "memory_reload_failed",
                "message": message,
                "rollback": {
                    "file_restored": rollback_file,
                    "runtime_restored": rollback_runtime,
                    "runtime_synchronized": runtime_synchronized,
                    "superseded_by_newer_config": superseded_by_newer_config,
                    "error": rollback_error,
                },
            },
        ) from exc


def _persist_memory_and_reload(
    *,
    path: Path,
    config_data: dict[str, Any],
    previous_raw: dict[str, Any],
    previous_present: bool,
    candidate_raw: dict[str, Any],
    reload: bool,
) -> dict[str, Any] | None:
    """Persist and apply Memory config in one worker-thread operation."""
    _atomic_write_config(config_data, path=path)
    return _reload_memory_with_rollback(
        path=path,
        previous_raw=previous_raw,
        previous_present=previous_present,
        candidate_raw=candidate_raw,
        reload=reload,
    )


def _model_summary(model: ModelConfig) -> dict[str, Any]:
    return {
        "name": model.name,
        "display_name": model.display_name or model.name,
        "supports_thinking": bool(model.supports_thinking),
        "supports_vision": bool(model.supports_vision),
    }


def _admin_config_response(raw_config: dict[str, Any], path: Path) -> dict[str, Any]:
    stat = path.stat()
    config_version = raw_config.get("config_version")
    api_config = raw_config.get("api") if isinstance(raw_config.get("api"), dict) else {}
    raw_models = raw_config.get("models") if isinstance(raw_config.get("models"), list) else []

    try:
        app_config = _validate_config_data(raw_config, source_path=path)
        skills_root = str(app_config.skills.get_skills_path())
    except Exception as exc:
        logger.debug(
            "Failed to validate app config while building Admin response: %s",
            _safe_config_error_message(exc, raw_config),
        )
        skills_root = None

    try:
        extensions_path = ExtensionsConfig.resolve_config_path()
        extensions_config_path = str(extensions_path) if extensions_path else None
    except Exception:
        logger.debug("Failed to resolve extensions config path", exc_info=True)
        extensions_config_path = None

    sandbox = raw_config.get("sandbox") if isinstance(raw_config.get("sandbox"), dict) else {}
    stream_bridge = raw_config.get("stream_bridge") if isinstance(raw_config.get("stream_bridge"), dict) else {}
    acp_agents = raw_config.get("acp_agents") if isinstance(raw_config.get("acp_agents"), dict) else {}
    raw_tools = raw_config.get("tools") if isinstance(raw_config.get("tools"), list) else []

    return {
        "config_path": str(path),
        "config_version": config_version,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "api": _redact_value("api", api_config),
        "models": [_redact_value("model", model) for model in raw_models if isinstance(model, dict)],
        "default_model": raw_config.get("default_model"),
        "system_summary": {
            "sandbox": {
                "use": sandbox.get("use"),
                "image": sandbox.get("image"),
                "replicas": sandbox.get("replicas"),
                "idle_timeout": sandbox.get("idle_timeout"),
                "mounts_count": len(sandbox.get("mounts") or []) if isinstance(sandbox.get("mounts"), list) else 0,
                "environment_keys": sorted(str(key) for key in (sandbox.get("environment") or {}).keys())
                if isinstance(sandbox.get("environment"), dict)
                else [],
                "security_opt": sandbox.get("security_opt") if isinstance(sandbox.get("security_opt"), list) else [],
            },
            "stream_bridge": {
                "type": stream_bridge.get("type", "memory"),
                "queue_maxsize": stream_bridge.get("queue_maxsize"),
                "redis_configured": bool(stream_bridge.get("redis_url")),
                "redis_maxlen": stream_bridge.get("redis_maxlen"),
                "redis_retention_seconds": stream_bridge.get("redis_retention_seconds"),
                "reconnect_grace_seconds": stream_bridge.get("reconnect_grace_seconds"),
                "completed_replay_seconds": stream_bridge.get("completed_replay_seconds"),
                "run_metadata_retention_seconds": stream_bridge.get("run_metadata_retention_seconds"),
            },
            "acp_agents": [
                {
                    "name": str(name),
                    "description": config.get("description"),
                    "model": config.get("model"),
                    "auto_approve_permissions": bool(config.get("auto_approve_permissions")),
                    # Surface the effective invocation timeout (ACPAgentConfig defaults
                    # to 600s; an explicit null means "wait indefinitely") so operators
                    # can tell a hung ACP agent from a slow one without reading the file.
                    "timeout_seconds": config.get("timeout_seconds", 600),
                    "artifact_allow_insecure_http": bool(
                        config.get("artifact_allow_insecure_http", False)
                    ),
                    "environment_keys": sorted(str(key) for key in (config.get("env") or {}).keys())
                    if isinstance(config.get("env"), dict)
                    else [],
                }
                for name, config in acp_agents.items()
                if isinstance(config, dict)
            ],
            "tools": [
                {
                    "name": tool.get("name"),
                    "group": tool.get("group"),
                    "use": tool.get("use"),
                    "configured_secret_fields": sorted(
                        str(key) for key, value in tool.items() if _is_secret_key(str(key)) and value not in (None, "")
                    ),
                }
                for tool in raw_tools
                if isinstance(tool, dict)
            ],
        },
        "paths": {
            "skills_root": skills_root,
            "extensions_config": extensions_config_path,
            "data_dir": api_config.get("data_dir") if isinstance(api_config, dict) else None,
        },
    }


def _raw_feishu_config(config_data: dict[str, Any]) -> dict[str, Any]:
    api_config = config_data.get("api") if isinstance(config_data.get("api"), dict) else {}
    raw = api_config.get("feishu") if isinstance(api_config, dict) else None
    return dict(raw) if isinstance(raw, dict) else {}


def _normalize_feishu_config(raw: dict[str, Any]) -> dict[str, Any]:
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        enabled = str(enabled).strip().lower() in {"1", "true", "yes", "on"}
    return {
        "enabled": enabled,
        "app_id": str(raw.get("app_id") or ""),
        "app_secret": str(raw.get("app_secret") or ""),
        "verification_token": str(raw.get("verification_token") or ""),
    }


def _resolve_admin_scalar(value: Any, *, field: str) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        env_name = value[1:]
        env_value = os.environ.get(env_name)
        if env_value is None:
            raise HTTPException(status_code=400, detail=f"Environment variable {env_name} not found for {field}")
        return env_value
    return value


def _feishu_settings_from_config(raw: dict[str, Any], *, validate_start: bool) -> FeishuSettings | None:
    normalized = _normalize_feishu_config(raw)
    app_id = str(_resolve_admin_scalar(normalized["app_id"], field="api.feishu.app_id") or "")
    if not app_id and not normalized["enabled"]:
        return None

    app_secret = str(_resolve_admin_scalar(normalized["app_secret"], field="api.feishu.app_secret") or "")
    verification_token = str(
        _resolve_admin_scalar(normalized["verification_token"], field="api.feishu.verification_token") or ""
    )

    if validate_start and normalized["enabled"]:
        if not app_id:
            raise HTTPException(status_code=400, detail="Feishu app_id is required when enabled")
        if not app_secret:
            raise HTTPException(status_code=400, detail="Feishu app_secret is required when enabled")

    return FeishuSettings(
        enabled=bool(normalized["enabled"]),
        app_id=app_id,
        app_secret=app_secret,
        verification_token=verification_token,
    )


def _set_runtime_feishu_settings(raw: dict[str, Any], *, validate_start: bool) -> None:
    settings.feishu = _feishu_settings_from_config(raw, validate_start=validate_start)


def _feishu_response(config_data: dict[str, Any], *, restart_result: dict[str, Any] | None = None) -> dict[str, Any]:
    manager = get_client_manager()
    raw = _normalize_feishu_config(_raw_feishu_config(config_data))
    return {
        "config": _redact_value("feishu", raw),
        "runtime": manager.feishu_status(),
        "restart": restart_result,
    }


def _validate_models(raw_models: list[dict[str, Any]]) -> list[ModelConfig]:
    models: list[ModelConfig] = []
    names: set[str] = set()
    for raw in raw_models:
        try:
            model = ModelConfig.model_validate(raw)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid model config: {exc}") from exc
        if not model.name.strip():
            raise HTTPException(status_code=400, detail="Model name cannot be empty")
        if model.name in names:
            raise HTTPException(status_code=400, detail=f"Duplicate model name: {model.name}")
        names.add(model.name)
        models.append(model)
    if not models:
        raise HTTPException(status_code=400, detail="At least one model is required")
    return models


def _model_dump(model: ModelConfig) -> dict[str, Any]:
    return model.model_dump(exclude_none=True)


def _reload_after_config_write(*, reload: bool, include_extensions: bool = False) -> dict[str, Any] | None:
    if not reload:
        return None
    manager = get_client_manager()
    ext_path = _resolve_extensions_config_path(create=False) if include_extensions else None
    return manager.reload_runtime_config(
        include_extensions=include_extensions,
        reset_clients=True,
        extensions_config_path=str(ext_path) if ext_path else None,
    )


def _validated_model_config(
    config_data: dict[str, Any],
    raw_models: list[dict[str, Any]],
    default_model: str | None,
) -> tuple[list[ModelConfig], str]:
    models = _validate_models(raw_models)
    model_names = {model.name for model in models}
    resolved_default = default_model
    if resolved_default is None:
        current_default = config_data.get("default_model")
        resolved_default = current_default if current_default in model_names else models[0].name
    if resolved_default not in model_names:
        raise HTTPException(status_code=400, detail=f"default_model '{resolved_default}' is not in models")
    return models, resolved_default


def _write_validated_models(
    *,
    config_data: dict[str, Any],
    path: Path,
    raw_models: list[dict[str, Any]],
    default_model: str | None,
    reload: bool,
) -> tuple[list[ModelConfig], str, dict[str, Any] | None]:
    models, resolved_default = _validated_model_config(config_data, raw_models, default_model)
    config_data["models"] = [_model_dump(model) for model in models]
    config_data["default_model"] = resolved_default
    _atomic_write_config(config_data, path=path)
    try:
        reload_result = _reload_after_config_write(reload=reload, include_extensions=True)
    except Exception as exc:
        logger.exception("Admin model update wrote config but reload failed")
        raise HTTPException(status_code=500, detail=f"Config saved but reload failed: {exc}") from exc
    return models, resolved_default, reload_result


def _validate_title_prompt(prompt_template: str) -> None:
    allowed_fields = {"max_words", "user_msg", "assistant_msg"}
    try:
        fields = {
            field_name
            for _, field_name, _, _ in string.Formatter().parse(prompt_template)
            if field_name is not None
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid title prompt template: {exc}") from exc
    unknown = sorted(fields - allowed_fields)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported title prompt fields: {', '.join(unknown)}")


def _validate_configured_model_name(config_data: dict[str, Any], model_name: str | None, *, field: str) -> None:
    if model_name is None or model_name == "inherit":
        return
    configured = {
        str(model.get("name"))
        for model in config_data.get("models", [])
        if isinstance(model, dict) and model.get("name")
    }
    if model_name not in configured:
        raise HTTPException(status_code=400, detail=f"{field} '{model_name}' is not configured")


# Codex Responses API models only accept low/medium/high/xhigh (models/factory.py:179).
# Other models get reasoning_effort passed through as-is to the underlying SDK;
# that path also feeds OpenAI-style "minimal" (models/factory.py:143 sets it
# directly when thinking is disabled), which Codex does not accept. The two
# domains only share low/medium/high, so a single flat allowlist would either
# wrongly reject "minimal" for non-Codex models or wrongly accept "xhigh" for
# them -- the value domain has to be resolved per model family.
_CODEX_REASONING_EFFORT_VALUES = ("low", "medium", "high", "xhigh")
_STANDARD_REASONING_EFFORT_VALUES = ("minimal", "low", "medium", "high")
_REASONING_EFFORT_VALUES = tuple(sorted(set(_CODEX_REASONING_EFFORT_VALUES) | set(_STANDARD_REASONING_EFFORT_VALUES)))


def _find_configured_model(config_data: dict[str, Any], model_name: str) -> dict[str, Any] | None:
    for model in config_data.get("models", []):
        if isinstance(model, dict) and model.get("name") == model_name:
            return model
    return None


def _is_codex_model(raw_model: dict[str, Any]) -> bool | None:
    """Best-effort static check of whether a configured model resolves to
    CodexChatModel, mirroring models/factory.py's own
    ``issubclass(model_class, CodexChatModel)`` check (factory.py:171).

    Returns None if the model's ``use`` path can't be resolved at admin-write
    time (missing/malformed field, import failure, etc.) -- callers should
    treat that the same as an unresolvable model and fall back to the
    permissive union of reasoning_effort values.
    """
    use_path = raw_model.get("use")
    if not isinstance(use_path, str) or not use_path:
        return None
    try:
        from langchain.chat_models import BaseChatModel

        from deerflow.models.openai_codex_provider import CodexChatModel
        from deerflow.reflection import resolve_class

        model_class = resolve_class(use_path, BaseChatModel)
    except (ImportError, ValueError):
        # resolve_class only raises ImportError (bad/missing module path) or
        # ValueError (not a class, or not a BaseChatModel subclass) --
        # deerflow/reflection/resolvers.py.
        return None
    return issubclass(model_class, CodexChatModel)


def _reasoning_effort_values_for(raw_model: dict[str, Any] | None) -> tuple[str, ...]:
    """Reasoning-effort value domain for a (possibly unresolved) model."""
    if raw_model is None:
        return _REASONING_EFFORT_VALUES
    is_codex = _is_codex_model(raw_model)
    if is_codex is True:
        return _CODEX_REASONING_EFFORT_VALUES
    if is_codex is False:
        return _STANDARD_REASONING_EFFORT_VALUES
    return _REASONING_EFFORT_VALUES


def _validate_subagent_generation_settings(
    config_data: dict[str, Any],
    *,
    model_name: str | None,
    thinking_enabled: bool | None,
    reasoning_effort: str | None,
    field: str,
) -> None:
    """Cross-check a per-agent thinking_enabled/reasoning_effort override against
    the resolved model's declared capabilities (supports_thinking / supports_reasoning_effort).

    ``model_name`` is the *effective* model for this agent, i.e. only set when
    the agent explicitly overrides its model (SubagentOverrideConfig.model is
    not None, or CustomSubagentConfig.model is not "inherit"). When the agent
    has no per-agent model override, it inherits whichever model the parent
    run happens to use -- that's not knowable at config-write time, so we
    skip the capability check and allow the override through. models/factory.py
    still degrades gracefully (warns + falls back to non-thinking) if the
    resolved model turns out not to support it at run time, so this is a
    best-effort admin-time check, not the only guardrail.

    The reasoning_effort *value-domain* check (as opposed to the
    supports_reasoning_effort capability check below) always runs when
    reasoning_effort is set, regardless of model resolvability -- only the
    allowed domain itself narrows once the model is statically known to be
    Codex or non-Codex (see _reasoning_effort_values_for).
    """
    raw_model = _find_configured_model(config_data, model_name) if model_name not in (None, "inherit") else None

    if reasoning_effort is not None:
        allowed_values = _reasoning_effort_values_for(raw_model)
        if reasoning_effort not in allowed_values:
            raise HTTPException(
                status_code=400,
                detail=f"{field}.reasoning_effort must be one of {allowed_values}, got '{reasoning_effort}'",
            )

    if raw_model is None:
        # Model override absent/inherited, or an unknown model name (which
        # _validate_configured_model_name already rejects separately).
        return

    if thinking_enabled and not raw_model.get("supports_thinking", False):
        raise HTTPException(
            status_code=400,
            detail=f"{field}.thinking_enabled requires model '{model_name}' to have supports_thinking: true",
        )
    if reasoning_effort is not None and not raw_model.get("supports_reasoning_effort", False):
        raise HTTPException(
            status_code=400,
            detail=f"{field}.reasoning_effort requires model '{model_name}' to have supports_reasoning_effort: true",
        )


def _validate_subagent_models(config_data: dict[str, Any], config: SubagentsAppConfig) -> None:
    for name, override in config.agents.items():
        field = f"subagents.agents.{name}"
        _validate_configured_model_name(config_data, override.model, field=f"{field}.model")
        _validate_subagent_generation_settings(
            config_data,
            model_name=override.model,
            thinking_enabled=override.thinking_enabled,
            reasoning_effort=override.reasoning_effort,
            field=field,
        )
    for name, custom in config.custom_agents.items():
        field = f"subagents.custom_agents.{name}"
        _validate_configured_model_name(config_data, custom.model, field=f"{field}.model")
        _validate_subagent_generation_settings(
            config_data,
            model_name=custom.model,
            thinking_enabled=custom.thinking_enabled,
            reasoning_effort=custom.reasoning_effort,
            field=field,
        )


def _admin_subagents_response(
    config: SubagentsAppConfig,
    *,
    include_write_metadata: bool = False,
    reload_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the admin payload with stable metadata for the visual editor."""
    payload: dict[str, Any] = {
        "config": config.model_dump(),
        "builtin_agents": [
            {
                "name": name,
                "description": builtin.description,
                "default_model": builtin.model,
            }
            for name, builtin in BUILTIN_SUBAGENTS.items()
        ],
    }
    if include_write_metadata:
        payload.update({"success": True, "reload": reload_result})
    return payload


def _validate_context_size(value: ContextSize, *, field: str) -> None:
    if value.type == "fraction":
        if not isinstance(value.value, (int, float)) or not 0 < float(value.value) <= 1:
            raise HTTPException(status_code=400, detail=f"{field}.value must be greater than 0 and at most 1")
        return
    if not isinstance(value.value, (int, float)) or float(value.value) < 1 or float(value.value) % 1 != 0:
        raise HTTPException(status_code=400, detail=f"{field}.value must be a positive integer")


def _validate_summarization_config(config: SummarizationConfig) -> None:
    triggers = config.trigger if isinstance(config.trigger, list) else [config.trigger] if config.trigger else []
    if config.enabled and not triggers:
        raise HTTPException(status_code=400, detail="summarization.trigger is required when summarization.enabled is true")
    for index, trigger in enumerate(triggers):
        _validate_context_size(trigger, field=f"summarization.trigger[{index}]")
    _validate_context_size(config.keep, field="summarization.keep")


def _config_example_path(config_path: Path) -> Path | None:
    candidates = [config_path.parent / "config.example.yaml", Path(__file__).resolve().parents[2] / "config.example.yaml"]
    return next((candidate for candidate in candidates if candidate.exists()), None)


def _config_version(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _validation_errors(exc: Exception) -> list[dict[str, Any]]:
    if isinstance(exc, ValidationError):
        return [
            {
                "path": ".".join(str(part) for part in error.get("loc", ())),
                "type": error.get("type", "validation_error"),
                "message": error.get("msg", "Invalid configuration value"),
            }
            for error in exc.errors(include_input=False, include_url=False)
        ]
    return [{"path": "", "type": type(exc).__name__, "message": str(exc)}]


def _literal_secret_summary(value: Any, *, section: str | None = None) -> tuple[int, set[str]]:
    count = 0
    sections: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            current_section = section or str(key)
            if _is_secret_key(str(key)):
                if item not in (None, "") and not (isinstance(item, str) and item.strip().startswith(("$", "${"))):
                    count += 1
                    sections.add(current_section)
                continue
            nested_count, nested_sections = _literal_secret_summary(item, section=current_section)
            count += nested_count
            sections.update(nested_sections)
    elif isinstance(value, list):
        for item in value:
            nested_count, nested_sections = _literal_secret_summary(item, section=section)
            count += nested_count
            sections.update(nested_sections)
    return count, sections


def _config_health_response(config_data: dict[str, Any], path: Path) -> dict[str, Any]:
    warnings: list[dict[str, str]] = []
    validation_errors: list[dict[str, Any]] = []
    try:
        _validate_config_data(config_data, source_path=path)
        valid = True
    except Exception as exc:
        valid = False
        validation_errors = _validation_errors(exc)
        for error in validation_errors:
            error["message"] = _redact_config_error_text(str(error["message"]), config_data)
        logger.debug(
            "Admin config health validation failed: %s",
            _safe_config_error_message(exc, config_data),
        )

    example_path = _config_example_path(path)
    example_data: dict[str, Any] = {}
    if example_path is not None:
        try:
            loaded = yaml.safe_load(example_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                example_data = loaded
        except Exception:
            logger.debug("Failed to read config.example.yaml for admin health", exc_info=True)

    current_version = _config_version(config_data.get("config_version"))
    latest_version = _config_version(example_data.get("config_version")) or current_version
    if current_version < latest_version:
        warnings.append(
            {
                "code": "config_outdated",
                "severity": "warning",
                "path": "config_version",
                "message": f"config.yaml version {current_version} is older than {latest_version}",
            }
        )

    title = config_data.get("title") if isinstance(config_data.get("title"), dict) else {}
    if "model" in title:
        warnings.append(
            {
                "code": "legacy_title_model",
                "severity": "warning",
                "path": "title.model",
                "message": "Use title.model_name; title.model is retained only for compatibility",
            }
        )

    guardrails = config_data.get("guardrails") if isinstance(config_data.get("guardrails"), dict) else {}
    if "providers" in guardrails and "provider" not in guardrails:
        warnings.append(
            {
                "code": "guardrails_provider_contract",
                "severity": "warning",
                "path": "guardrails.providers",
                "message": "Runtime currently expects guardrails.provider rather than guardrails.providers",
            }
        )

    tracing = config_data.get("tracing") if isinstance(config_data.get("tracing"), dict) else {}
    if "enabled" in tracing:
        warnings.append(
            {
                "code": "tracing_top_level_enabled_ignored",
                "severity": "info",
                "path": "tracing.enabled",
                "message": "Tracing effective state is controlled by each provider's enabled field",
            }
        )

    configured_models = {
        str(model.get("name"))
        for model in config_data.get("models", [])
        if isinstance(model, dict) and model.get("name")
    }
    title_model = title.get("model_name") or title.get("model")
    if title_model and title_model not in configured_models:
        warnings.append(
            {
                "code": "title_model_missing",
                "severity": "warning",
                "path": "title.model_name",
                "message": "Title model does not reference a configured model name",
            }
        )

    missing_sections = sorted(
        key for key, value in example_data.items() if isinstance(value, (dict, list)) and key not in config_data
    )
    unknown_sections = sorted(str(key) for key in config_data if key not in _KNOWN_TOP_LEVEL_CONFIG_KEYS)
    literal_secret_count, literal_secret_sections = _literal_secret_summary(config_data)
    file_writable = os.access(path, os.W_OK)
    directory_writable = os.access(path.parent, os.W_OK)
    if not file_writable or not directory_writable:
        warnings.append(
            {
                "code": "config_not_atomically_writable",
                "severity": "warning",
                "path": "",
                "message": "config.yaml and its parent directory must both be writable for atomic Admin saves",
            }
        )
    if literal_secret_count:
        warnings.append(
            {
                "code": "literal_secrets",
                "severity": "info",
                "path": "",
                "message": f"{literal_secret_count} literal secret value(s) are stored in config.yaml",
            }
        )

    if not valid:
        status = "error"
    elif any(warning["severity"] == "warning" for warning in warnings):
        status = "warning"
    else:
        status = "ok"
    return {
        "status": status,
        "valid": valid,
        "config_path": str(path),
        "writable": file_writable and directory_writable,
        "file_writable": file_writable,
        "directory_writable": directory_writable,
        "current_version": current_version,
        "latest_version": latest_version,
        "outdated": current_version < latest_version,
        "missing_sections": missing_sections,
        "unknown_sections": unknown_sections,
        "literal_secrets": {"count": literal_secret_count, "sections": sorted(literal_secret_sections)},
        "warnings": warnings,
        "validation_errors": validation_errors,
    }


def _runtime_changes(req: AdminRuntimePatchRequest) -> dict[str, Any]:
    fields = req.model_fields_set - {"reload"}
    changes = {field: getattr(req, field) for field in fields}
    if "allowed_upload_extensions" in changes:
        extensions = changes["allowed_upload_extensions"] or []
        normalized = []
        for ext in extensions:
            value = str(ext).strip().lower()
            if not value:
                continue
            if not value.startswith("."):
                value = f".{value}"
            normalized.append(value)
        changes["allowed_upload_extensions"] = sorted(set(normalized))
    return changes


def _validate_runtime_changes(changes: dict[str, Any], config_data: dict[str, Any]) -> None:
    model_name = changes.get("model_name")
    if model_name is not None:
        model_names = {
            str(model.get("name"))
            for model in config_data.get("models", [])
            if isinstance(model, dict) and model.get("name")
        }
        if model_name not in model_names:
            raise HTTPException(status_code=400, detail=f"model_name '{model_name}' is not configured")


def _apply_runtime_settings(changes: dict[str, Any]) -> dict[str, str]:
    effects: dict[str, str] = {}
    for field, value in changes.items():
        if field in _HOT_RUNTIME_FIELDS:
            setattr(settings, field, value)
            effects[field] = "hot_applied"
        elif field in _RESTART_RUNTIME_FIELDS:
            effects[field] = "requires_restart"
    return effects


def _mcp_servers_from_extensions(*, create: bool = False) -> tuple[Path | None, dict[str, Any], dict[str, Any]]:
    path, data = _load_extensions_data(create=create)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise HTTPException(status_code=500, detail="extensions mcpServers must be an object")
    return path, data, servers


def _write_mcp_servers(path: Path | None, data: dict[str, Any], servers: dict[str, Any]) -> None:
    from app.routers.mcp import _validate_mcp_servers

    _validate_mcp_servers(servers)
    data["mcpServers"] = servers
    _write_extensions_data(path, data)


async def _probe_remote_mcp(url: str, timeout_seconds: float) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
            response = await client.head(url)
            if response.status_code in {405, 501}:
                response = await client.get(url, headers={"Range": "bytes=0-0"})
        return {
            "ok": response.status_code < 500,
            "status": "reachable" if response.status_code < 500 else "server_error",
            "http_status": response.status_code,
        }
    except httpx.TimeoutException:
        return {"ok": False, "status": "timeout"}
    except httpx.HTTPError as exc:
        return {"ok": False, "status": "connection_failed", "error": exc.__class__.__name__}


@router.get("/me")
async def admin_me():
    """Return Admin API identity and capability metadata."""
    return {
        "authenticated": True,
        "auth": {
            "type": "bearer",
            "auth_enabled": settings.auth_enabled,
        },
        "capabilities": {
            "config_read": True,
            "config_reload": True,
            "models_write": True,
            "model_patch": True,
            "title_write": True,
            "subagents_write": True,
            "memory_write": True,
            "summarization_write": True,
            "config_health": True,
            "scheduled_tasks_read": True,
            "scheduled_tasks_delete": True,
            "custom_skills_write": True,
            "runtime_write": True,
            "mcp_admin": True,
            "feishu_admin": True,
            "thread_cleanup_admin": True,
        },
    }


def _thread_cleanup_service():
    service = get_client_manager().thread_cleanup_service
    if service is None:
        raise HTTPException(status_code=409, detail="Thread cleanup requires the SQLite checkpointer")
    return service


@router.get("/thread-cleanup/config")
async def get_thread_cleanup_config():
    """Return validated inactive-thread cleanup configuration."""
    return {"config": settings.thread_cleanup.model_dump()}


@router.put("/thread-cleanup/config")
async def update_thread_cleanup_config(req: AdminThreadCleanupUpdateRequest = Body()):
    """Persist and hot-apply inactive-thread cleanup configuration."""
    manager = get_client_manager()
    if manager.thread_cleanup_service is None and settings.checkpointer_type != "sqlite":
        raise HTTPException(status_code=409, detail="Thread cleanup requires the SQLite checkpointer")
    path = _config_path()
    config_data = _load_config_data(path)
    api_config = config_data.setdefault("api", {})
    if not isinstance(api_config, dict):
        raise HTTPException(status_code=400, detail="api section must be an object")
    api_config["thread_cleanup"] = req.config.model_dump()
    _atomic_write_config(config_data, path=path)
    settings.thread_cleanup = req.config
    try:
        await manager.configure_thread_cleanup(req.config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "config": req.config.model_dump(), "effect": "hot_applied"}


@router.get("/thread-cleanup/status")
async def get_thread_cleanup_status():
    """Return cleanup schedule, active job and SQLite space metrics."""
    return await _thread_cleanup_service().status()


@router.get("/thread-cleanup/preview")
async def preview_thread_cleanup(limit: int = Query(default=100, ge=1, le=500)):
    """Preview inactive cleanup candidates without deleting them."""
    return await _thread_cleanup_service().preview(limit=limit)


@router.post("/thread-cleanup/runs", status_code=status.HTTP_202_ACCEPTED)
async def start_thread_cleanup_run(req: AdminThreadCleanupRunRequest = Body(default_factory=AdminThreadCleanupRunRequest)):
    """Start a background cleanup job and return immediately."""
    return await _thread_cleanup_service().start_run(
        trigger="manual",
        dry_run=req.dry_run,
        limit=req.limit,
    )


@router.get("/config")
async def get_admin_config():
    """Return a redacted view of the file-backed service configuration."""
    path = _config_path()
    raw_config = _load_config_data(path)
    return _admin_config_response(raw_config, path)


@router.get("/config/health")
async def get_admin_config_health():
    """Validate config.yaml and return safe compatibility/security diagnostics."""
    path = _config_path()
    return _config_health_response(_load_config_data(path), path)


@router.get("/scheduled-tasks")
async def get_admin_scheduled_tasks(
    include_disabled: bool = True,
    thread_id: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=50, ge=1, le=200),
):
    """List persisted scheduled tasks without exposing internal metadata or kwargs."""
    store, db_path, scheduler_enabled = _admin_scheduler_store()
    if store is None:
        return {
            "tasks": [],
            "count": 0,
            "scheduler_enabled": scheduler_enabled,
            "storage_exists": False,
        }
    try:
        tasks = await store.list_tasks(thread_id=thread_id, include_disabled=include_disabled, limit=limit)
    except Exception as exc:
        logger.exception("Admin scheduled task listing failed for %s", db_path)
        raise HTTPException(status_code=500, detail=f"Failed to read scheduled tasks: {exc}") from exc
    return {
        "tasks": [_admin_scheduled_task_response(task) for task in tasks],
        "count": len(tasks),
        "scheduler_enabled": scheduler_enabled,
        "storage_exists": True,
    }


@router.delete("/scheduled-tasks/{task_id}")
async def delete_admin_scheduled_task(task_id: str):
    """Delete a persisted scheduled task and its stored execution records."""
    store, db_path, _scheduler_enabled = _admin_scheduler_store()
    if store is None:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    try:
        task = await store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Scheduled task not found")
        if not await store.delete_task(task_id):
            raise HTTPException(status_code=404, detail="Scheduled task not found")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Admin scheduled task deletion failed for %s in %s", task_id, db_path)
        raise HTTPException(status_code=500, detail=f"Failed to delete scheduled task: {exc}") from exc
    return {"success": True, "deleted": task_id}


@router.get("/title")
async def get_admin_title():
    """Return validated automatic-title configuration."""
    config_data = _load_config_data(_config_path())
    config = TitleConfig.model_validate(config_data.get("title") or {})
    return {"config": config.model_dump()}


@router.put("/title")
async def update_admin_title(req: AdminTitleUpdateRequest = Body()):
    """Update automatic-title configuration and optionally reload clients."""
    path = _config_path()
    config_data = _load_config_data(path)
    _validate_title_prompt(req.config.prompt_template)
    _validate_configured_model_name(config_data, req.config.model_name, field="title.model_name")
    config_data["title"] = req.config.model_dump(exclude_none=True)
    _atomic_write_config(config_data, path=path)
    try:
        reload_result = _reload_after_config_write(reload=req.reload)
    except Exception as exc:
        logger.exception("Admin title update wrote config but reload failed")
        raise HTTPException(status_code=500, detail=f"Title config saved but reload failed: {exc}") from exc
    return {"success": True, "config": req.config.model_dump(), "reload": reload_result}


@router.get("/subagents")
async def get_admin_subagents():
    """Return validated built-in and custom subagent configuration."""
    config_data = _load_config_data(_config_path())
    config = SubagentsAppConfig.model_validate(config_data.get("subagents") or {})
    return _admin_subagents_response(config)


@router.put("/subagents")
async def update_admin_subagents(req: AdminSubagentsUpdateRequest = Body()):
    """Update subagent configuration and optionally reload clients."""
    path = _config_path()
    config_data = _load_config_data(path)
    _validate_subagent_models(config_data, req.config)
    config_data["subagents"] = req.config.model_dump(exclude_none=True)
    _atomic_write_config(config_data, path=path)
    try:
        reload_result = _reload_after_config_write(reload=req.reload)
    except Exception as exc:
        logger.exception("Admin subagents update wrote config but reload failed")
        raise HTTPException(status_code=500, detail=f"Subagents config saved but reload failed: {exc}") from exc
    return _admin_subagents_response(req.config, include_write_metadata=True, reload_result=reload_result)


@router.get("/memory")
async def get_admin_memory():
    """Return validated global-memory configuration."""
    config_data = _load_config_data(_config_path())
    return _memory_admin_response(config_data)


@router.get("/memory/capabilities")
async def get_admin_memory_capabilities():
    """Return stable UI metadata and generated validation schemas."""
    return _memory_capabilities_response()


@router.post("/memory/validate")
async def validate_admin_memory(req: AdminMemoryCandidateRequest = Body()):
    """Validate a complete candidate without writing or opening a backend."""
    config_data = _load_config_data(_config_path())
    current = MemoryConfig.model_validate(_memory_raw_config(config_data))
    current_canonical = _memory_canonical_dump(current)
    restored = _restore_redacted_values(req.config, current_canonical, path="memory")
    config = _validate_memory_candidate(config_data, restored)
    warnings: list[dict[str, str]] = []
    if config.mode == "tool" and config.injection_enabled:
        warnings.append(
            {
                "code": "injection_ignored_in_tool_mode",
                "message": "injection_enabled is preserved but ignored while mode is 'tool'.",
            }
        )
    if config.manager_class != current.manager_class:
        warnings.append(
            {
                "code": "memory_backend_migration_not_automatic",
                "message": "Changing memory backend does not migrate existing memory data.",
            }
        )
    return {
        "valid": True,
        "canonical_config": _redact_value("memory", _memory_canonical_dump(config)),
        "warnings": warnings,
    }


@router.post("/memory/test")
async def test_admin_memory(req: AdminMemoryCandidateRequest = Body()):
    """Construct and warm an isolated candidate backend without changing runtime state."""
    config_data = _load_config_data(_config_path())
    current = MemoryConfig.model_validate(_memory_raw_config(config_data))
    restored = _restore_redacted_values(req.config, _memory_canonical_dump(current), path="memory")
    config = _validate_memory_candidate(config_data, restored)
    probe = await _probe_memory_candidate_async(config)
    credentials = None
    if config.manager_class == "mem0":
        env_name = str(config.backend_config.get("api_key_env") or "")
        credentials = {"api_key_env": env_name, "env_present": bool(env_name and os.environ.get(env_name))}
    return {
        "success": True,
        "probe": probe,
        "credentials": credentials,
        "canonical_config": _redact_value("memory", _memory_canonical_dump(config)),
    }


@router.put("/memory")
async def update_admin_memory(req: AdminMemoryUpdateRequest = Body()):
    """Compatibility full replacement using the legacy serializable shape."""
    path = _config_path()
    config_data = _load_config_data(path)
    previous_present = "memory" in config_data
    previous_raw = _memory_raw_config(config_data)
    current = MemoryConfig.model_validate(previous_raw)
    restored = _restore_redacted_values(req.config, current.model_dump(), path="memory")
    config = _validate_memory_candidate(config_data, restored)
    persisted_memory = config.model_dump(exclude_none=True)
    config_data["memory"] = persisted_memory
    reload_result = await asyncio.to_thread(
        _persist_memory_and_reload,
        path=path,
        config_data=config_data,
        previous_raw=previous_raw,
        previous_present=previous_present,
        candidate_raw=persisted_memory,
        reload=req.reload,
    )
    response = _memory_admin_response(_load_config_data(path), reload_result=reload_result)
    return {"success": True, **response}


@router.patch("/memory")
async def patch_admin_memory(req: AdminMemoryPatchRequest = Body()):
    """Safely update canonical memory configuration with optimistic concurrency."""
    unknown = sorted(str(key) for key in req.changes if key not in _MEMORY_CANONICAL_KEYS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"code": "unknown_memory_fields", "fields": unknown},
        )

    path = _config_path()
    config_data = _load_config_data(path)
    previous_present = "memory" in config_data
    previous_raw = _memory_raw_config(config_data)
    current_revision = _memory_revision(previous_raw)
    if req.expected_revision != current_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "memory_revision_conflict",
                "message": "Memory config changed after it was loaded; refresh and retry.",
                "current_revision": current_revision,
            },
        )

    current = MemoryConfig.model_validate(previous_raw)
    current_canonical = _memory_canonical_dump(current)
    requested_manager = str(req.changes.get("manager_class", current.manager_class)).strip()
    manager_changed = requested_manager != current.manager_class
    if manager_changed and req.backend_config_mode != "replace":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "backend_replace_required",
                "message": "backend_config_mode='replace' is required when changing manager_class.",
            },
        )

    candidate = deepcopy(current_canonical)
    for key, value in req.changes.items():
        if key == "backend_config":
            if not isinstance(value, dict):
                raise HTTPException(status_code=400, detail="memory.backend_config must be an object")
            existing_backend = {} if manager_changed else current_canonical["backend_config"]
            restored_backend = _restore_redacted_values(value, existing_backend, path="memory.backend_config")
            candidate["backend_config"] = (
                restored_backend
                if req.backend_config_mode == "replace"
                else _deep_merge_mapping(existing_backend, restored_backend)
            )
        else:
            candidate[key] = deepcopy(value)

    config = _validate_memory_candidate(config_data, candidate)
    probe_result = await _probe_memory_candidate_async(config) if req.probe else None
    config_data["memory"] = _memory_canonical_dump(config)
    reload_result = await asyncio.to_thread(
        _persist_memory_and_reload,
        path=path,
        config_data=config_data,
        previous_raw=previous_raw,
        previous_present=previous_present,
        candidate_raw=_memory_canonical_dump(config),
        reload=req.reload,
    )
    response = _memory_admin_response(_load_config_data(path), reload_result=reload_result)
    return {"success": True, "probe": probe_result, **response}


@router.get("/summarization")
async def get_admin_summarization():
    """Return validated conversation-summarization configuration."""
    config_data = _load_config_data(_config_path())
    config = SummarizationConfig.model_validate(config_data.get("summarization") or {})
    return {"config": config.model_dump()}


@router.put("/summarization")
async def update_admin_summarization(req: AdminSummarizationUpdateRequest = Body()):
    """Update conversation-summarization configuration and reload new clients."""
    path = _config_path()
    config_data = _load_config_data(path)
    _validate_configured_model_name(config_data, req.config.model_name, field="summarization.model_name")
    _validate_summarization_config(req.config)
    config_data["summarization"] = req.config.model_dump(exclude_none=True)
    _atomic_write_config(config_data, path=path)
    try:
        reload_result = _reload_after_config_write(reload=req.reload)
    except Exception as exc:
        logger.exception("Admin summarization update wrote config but reload failed")
        raise HTTPException(status_code=500, detail=f"Summarization config saved but reload failed: {exc}") from exc
    return {"success": True, "config": req.config.model_dump(), "reload": reload_result}


@router.put("/models")
async def update_admin_models(req: AdminModelsUpdateRequest = Body()):
    """Replace configured models and optionally reload runtime config."""
    path = _config_path()
    config_data = _load_config_data(path)

    existing_models = {
        str(model.get("name")): model
        for model in config_data.get("models", [])
        if isinstance(model, dict) and model.get("name")
    }
    restored_models = []
    for model in req.models:
        name = model.get("name")
        existing = existing_models.get(str(name)) if name is not None else None
        incoming = dict(model)
        # Compatibility safety: older clients replace the full models list. If
        # they omit api_key, preserve the stored value instead of deleting it.
        if existing is not None and "api_key" not in incoming and "api_key" in existing:
            incoming["api_key"] = existing["api_key"]
        restored_models.append(_restore_redacted_values(incoming, existing, path=str(name or "<unnamed-model>")))

    try:
        models, default_model, reload_result = _write_validated_models(
            config_data=config_data,
            path=path,
            raw_models=restored_models,
            default_model=req.default_model,
            reload=req.reload,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Admin model update wrote config but reload failed")
        raise HTTPException(status_code=500, detail=f"Config saved but reload failed: {exc}") from exc

    return {
        "success": True,
        "models": [_model_summary(model) for model in models],
        "default_model": default_model,
        "reloaded": bool(req.reload),
        "reload": reload_result,
    }


@router.post("/models")
async def create_admin_model(req: AdminModelCreateRequest = Body()):
    """Create one model without requiring clients to replace the full model list."""
    path = _config_path()
    config_data = _load_config_data(path)
    raw_models = [dict(model) for model in config_data.get("models", []) if isinstance(model, dict)]
    incoming = _restore_redacted_values(req.model, None, path=str(req.model.get("name") or "<new-model>"))
    candidate = _validate_models([incoming])[0]
    if any(model.get("name") == candidate.name for model in raw_models):
        raise HTTPException(status_code=409, detail=f"Model '{candidate.name}' already exists")
    raw_models.append(_model_dump(candidate))
    default_model = candidate.name if req.set_default or not config_data.get("default_model") else config_data.get("default_model")
    models, default_model, reload_result = _write_validated_models(
        config_data=config_data,
        path=path,
        raw_models=raw_models,
        default_model=default_model,
        reload=req.reload,
    )
    return {
        "success": True,
        "model": _model_summary(next(model for model in models if model.name == candidate.name)),
        "default_model": default_model,
        "reloaded": bool(req.reload),
        "reload": reload_result,
    }


@router.patch("/models/{model_name}")
async def patch_admin_model(model_name: str, req: AdminModelPatchRequest = Body()):
    """Patch one model; omitted fields, including api_key, retain their stored values."""
    if not req.changes and not req.clear_api_key and not req.set_default:
        raise HTTPException(status_code=400, detail="At least one model change is required")

    path = _config_path()
    config_data = _load_config_data(path)
    raw_models = [dict(model) for model in config_data.get("models", []) if isinstance(model, dict)]
    index = next((i for i, model in enumerate(raw_models) if str(model.get("name")) == model_name), None)
    if index is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found")

    existing = raw_models[index]
    changes = deepcopy(req.changes)
    if req.clear_api_key and changes.get("api_key") not in (None, ""):
        raise HTTPException(status_code=400, detail="api_key and clear_api_key cannot be used together")
    if changes.get("api_key") in (None, ""):
        changes.pop("api_key", None)

    merged = deepcopy(existing)
    for key, value in changes.items():
        merged[key] = _restore_redacted_values(value, existing.get(key), path=f"{model_name}.{key}")
    if req.clear_api_key:
        merged.pop("api_key", None)

    candidate = _validate_models([merged])[0]
    if candidate.name != model_name and any(
        i != index and str(model.get("name")) == candidate.name for i, model in enumerate(raw_models)
    ):
        raise HTTPException(status_code=409, detail=f"Model '{candidate.name}' already exists")
    raw_models[index] = _model_dump(candidate)

    default_model = config_data.get("default_model")
    if req.set_default or default_model == model_name:
        default_model = candidate.name
    models, default_model, reload_result = _write_validated_models(
        config_data=config_data,
        path=path,
        raw_models=raw_models,
        default_model=default_model,
        reload=req.reload,
    )
    return {
        "success": True,
        "model": _model_summary(next(model for model in models if model.name == candidate.name)),
        "default_model": default_model,
        "reloaded": bool(req.reload),
        "reload": reload_result,
    }


@router.delete("/models/{model_name}")
async def delete_admin_model(model_name: str, reload: bool = True):
    """Delete one model and select a safe replacement default when necessary."""
    path = _config_path()
    config_data = _load_config_data(path)
    raw_models = [dict(model) for model in config_data.get("models", []) if isinstance(model, dict)]
    kept = [model for model in raw_models if str(model.get("name")) != model_name]
    if len(kept) == len(raw_models):
        raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found")
    if not kept:
        raise HTTPException(status_code=400, detail="At least one model is required")
    default_model = config_data.get("default_model")
    if default_model == model_name:
        default_model = str(kept[0].get("name"))
    models, default_model, reload_result = _write_validated_models(
        config_data=config_data,
        path=path,
        raw_models=kept,
        default_model=default_model,
        reload=reload,
    )
    return {
        "success": True,
        "deleted": model_name,
        "models": [_model_summary(model) for model in models],
        "default_model": default_model,
        "reloaded": bool(reload),
        "reload": reload_result,
    }


@router.post("/config/reload")
async def reload_admin_config(req: AdminReloadRequest = Body(default_factory=AdminReloadRequest)):
    """Reload config from disk and clear stale runtime caches."""
    try:
        manager = get_client_manager()
        ext_path = _resolve_extensions_config_path(create=False)
        return manager.reload_runtime_config(
            include_extensions=req.include_extensions,
            reset_clients=req.reset_clients,
            extensions_config_path=str(ext_path) if ext_path else None,
        )
    except Exception as exc:
        logger.exception("Admin config reload failed")
        raise HTTPException(status_code=500, detail=f"Reload failed: {exc}") from exc


@router.get("/feishu")
async def get_admin_feishu():
    """Return redacted Feishu channel config and runtime status."""
    config_data = _load_config_data(_config_path())
    return _feishu_response(config_data)


@router.put("/feishu")
async def update_admin_feishu(req: AdminFeishuUpdateRequest = Body()):
    """Write Feishu channel config and optionally restart the channel."""
    path = _config_path()
    config_data = _load_config_data(path)
    api_config = config_data.setdefault("api", {})
    if not isinstance(api_config, dict):
        raise HTTPException(status_code=400, detail="api section must be an object")

    existing = api_config.get("feishu") if isinstance(api_config.get("feishu"), dict) else {}
    incoming = {
        "enabled": req.enabled,
        "app_id": req.app_id or "",
        "app_secret": req.app_secret if req.app_secret is not None else "",
        "verification_token": req.verification_token if req.verification_token is not None else "",
    }
    restored = _restore_redacted_values(incoming, existing, path="api.feishu")
    normalized = _normalize_feishu_config(restored)
    if req.restart:
        _ = _feishu_settings_from_config(normalized, validate_start=True)

    api_config["feishu"] = normalized
    _atomic_write_config(config_data, path=path)

    restart_result = None
    if req.restart:
        _set_runtime_feishu_settings(normalized, validate_start=True)
        try:
            restart_result = await get_client_manager().restart_feishu_channel(raise_on_error=True)
        except Exception as exc:
            logger.exception("Admin Feishu update wrote config but restart failed")
            raise HTTPException(status_code=500, detail=f"Feishu config saved but restart failed: {exc}") from exc

    return {"success": True, **_feishu_response(config_data, restart_result=restart_result)}


@router.post("/feishu/restart")
async def restart_admin_feishu():
    """Restart Feishu channel from the file-backed config."""
    config_data = _load_config_data(_config_path())
    raw = _normalize_feishu_config(_raw_feishu_config(config_data))
    _set_runtime_feishu_settings(raw, validate_start=True)
    try:
        restart_result = await get_client_manager().restart_feishu_channel(raise_on_error=True)
    except Exception as exc:
        logger.exception("Admin Feishu restart failed")
        raise HTTPException(status_code=500, detail=f"Feishu restart failed: {exc}") from exc
    return {"success": True, **_feishu_response(config_data, restart_result=restart_result)}


@router.get("/skills/custom")
async def list_admin_custom_skills():
    """List custom skills that can be edited through the Admin API."""
    with _admin_app_config_context():
        return {"skills": [_custom_skill_response(skill.name) for skill in list_custom_skills()]}


@router.get("/evolution/status")
async def get_admin_evolution_status():
    """Return Skill evolution, worker, signal and probation status."""
    with _admin_app_config_context():
        status = SkillEvolutionService().status()
        status["worker"] = get_evolution_worker().status()
        return status


@router.get("/evolution/signals")
async def list_admin_evolution_signals(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    """List sanitized automatic-discovery signals, newest first."""
    with _admin_app_config_context():
        store = get_evolution_store()
        signals = store.list_signals(status=status)[:limit]
        return {"signals": [signal.model_dump(mode="json", exclude={"tool_errors"}) for signal in signals]}


@router.get("/evolution/signals/{signal_id}")
async def get_admin_evolution_signal(signal_id: str):
    """Return one sanitized automatic-discovery signal with tool errors."""
    try:
        with _admin_app_config_context():
            signal = get_evolution_store().load_signal(signal_id)
            return signal.model_dump(mode="json")
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.delete("/evolution/signals/{signal_id}")
async def delete_admin_evolution_signal(signal_id: str):
    """Cancel a queued signal when needed, then remove its durable record."""
    try:
        with _admin_app_config_context():
            store = get_evolution_store()
            signal = store.load_signal(signal_id)
            if signal.status == "processing":
                raise HTTPException(status_code=409, detail="A processing evolution signal cannot be deleted.")
            if not get_evolution_worker().cancel(signal_id):
                raise HTTPException(status_code=409, detail="Evolution signal processing has already started.")
            deleted = store.delete_signal(signal_id)
            store.append_audit(
                actor="admin",
                action="signal.deleted",
                details={
                    "signal_id": deleted.id,
                    "status": deleted.status,
                    "proposal_id": deleted.proposal_id,
                },
            )
            return {
                "success": True,
                "signal_id": deleted.id,
                "status": deleted.status,
                "proposal_preserved": bool(deleted.proposal_id),
            }
    except HTTPException:
        raise
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.post("/evolution/observability/cleanup")
async def cleanup_admin_evolution_observability():
    """Delete all cancellable Signals and completed probation records."""
    try:
        with _admin_app_config_context():
            store = get_evolution_store()
            worker = get_evolution_worker()
            deleted_signal_ids: list[str] = []
            skipped_signals: list[dict[str, str]] = []
            preserved_proposal_ids: set[str] = set()

            for signal in store.list_signals():
                if signal.status == "processing":
                    skipped_signals.append(
                        {"signal_id": signal.id, "status": signal.status, "reason": "processing"}
                    )
                    continue
                if not worker.cancel(signal.id):
                    skipped_signals.append(
                        {"signal_id": signal.id, "status": signal.status, "reason": "processing_started"}
                    )
                    continue
                deleted = store.delete_signal(signal.id)
                deleted_signal_ids.append(deleted.id)
                if deleted.proposal_id:
                    preserved_proposal_ids.add(deleted.proposal_id)
                store.append_audit(
                    actor="admin",
                    action="signal.deleted",
                    details={
                        "signal_id": deleted.id,
                        "status": deleted.status,
                        "proposal_id": deleted.proposal_id,
                        "batch": True,
                    },
                )

            deleted_probations = store.delete_probations_by_status({"graduated"})
            for skill_name, probation in deleted_probations.items():
                store.append_audit(
                    actor="admin",
                    action="probation.cleaned",
                    details={
                        "skill_name": skill_name,
                        "revision": probation.get("revision"),
                        "status": probation.get("status"),
                    },
                )

            preserved_probation_counts: dict[str, int] = {}
            for probation in store.get_probations().values():
                probation_status = str(probation.get("status") or "unknown")
                preserved_probation_counts[probation_status] = (
                    preserved_probation_counts.get(probation_status, 0) + 1
                )

            return {
                "success": True,
                "deleted_signal_count": len(deleted_signal_ids),
                "deleted_signal_ids": deleted_signal_ids,
                "skipped_signal_count": len(skipped_signals),
                "skipped_signals": skipped_signals,
                "preserved_proposal_count": len(preserved_proposal_ids),
                "preserved_proposal_ids": sorted(preserved_proposal_ids),
                "deleted_probation_count": len(deleted_probations),
                "deleted_probations": sorted(deleted_probations),
                "preserved_probation_counts": preserved_probation_counts,
                "observations_preserved": True,
            }
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.get("/evolution/proposals")
async def list_admin_evolution_proposals(
    status: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
    archived_only: bool = Query(default=False),
):
    """List Skill proposals, newest first, hiding archived records by default."""
    with _admin_app_config_context():
        store = get_evolution_store()
        proposals = store.list_proposals(
            status=status,
            include_archived=include_archived or archived_only,
            archived_only=archived_only,
        )
        return {
            "proposals": [proposal.model_dump(mode="json") for proposal in proposals],
            "catalog_version": store.get_catalog_version(),
        }


@router.get("/evolution/proposals/{proposal_id}")
async def get_admin_evolution_proposal(proposal_id: str):
    """Return one Proposal with its unified diff."""
    try:
        with _admin_app_config_context():
            store = get_evolution_store()
            proposal = store.load_proposal(proposal_id)
            return {**proposal.model_dump(mode="json"), "diff": store.read_proposal_diff(proposal_id)}
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.post("/evolution/proposals/{proposal_id}/approve")
async def approve_admin_evolution_proposal(proposal_id: str, req: AdminProposalReviewRequest = Body(default=AdminProposalReviewRequest())):
    """Validate and atomically publish a pending Skill Proposal."""
    try:
        proposal = await approve_skill_proposal(
            proposal_id,
            expected_base_sha256=req.expected_base_sha256,
            note=req.note,
        )
        return {
            "success": True,
            "proposal": proposal.model_dump(mode="json"),
            "catalog_version": get_skill_catalog_version(),
        }
    except SkillPublishConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.post("/evolution/proposals/{proposal_id}/reject")
async def reject_admin_evolution_proposal(proposal_id: str, req: AdminProposalReviewRequest = Body(default=AdminProposalReviewRequest())):
    """Reject a pending Skill Proposal without changing active files."""
    try:
        proposal = reject_skill_proposal(proposal_id, note=req.note)
        return {"success": True, "proposal": proposal.model_dump(mode="json")}
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.post("/evolution/proposals/{proposal_id}/archive")
async def archive_admin_evolution_proposal(proposal_id: str):
    """Archive one terminal Proposal while preserving all linked records."""
    try:
        with _admin_app_config_context():
            proposal = SkillEvolutionService().archive_proposal(proposal_id)
            return {"success": True, "proposal": proposal.model_dump(mode="json")}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.post("/evolution/proposals/archive-batch")
async def archive_admin_evolution_proposals_batch(
    req: AdminProposalArchiveBatchRequest = Body(),
):
    """Archive an explicit set of terminal Proposals and report partial skips."""
    try:
        with _admin_app_config_context():
            store = get_evolution_store()
            service = SkillEvolutionService(store)
            proposal_ids = list(dict.fromkeys(req.proposal_ids))
            archived_ids: list[str] = []
            already_archived_ids: list[str] = []
            skipped: list[dict[str, str | None]] = []

            with store.lock:
                for proposal_id in proposal_ids:
                    try:
                        proposal = store.load_proposal(proposal_id)
                    except FileNotFoundError:
                        skipped.append(
                            {"proposal_id": proposal_id, "status": None, "reason": "not_found"}
                        )
                        continue
                    if proposal.archived_at is not None:
                        already_archived_ids.append(proposal.id)
                        continue
                    if proposal.status not in {"published", "rejected", "failed", "stale"}:
                        skipped.append(
                            {"proposal_id": proposal.id, "status": proposal.status, "reason": "not_terminal"}
                        )
                        continue
                    service.archive_proposal(proposal.id)
                    archived_ids.append(proposal.id)

            return {
                "success": True,
                "requested_count": len(proposal_ids),
                "archived_count": len(archived_ids),
                "archived_ids": archived_ids,
                "already_archived_count": len(already_archived_ids),
                "already_archived_ids": already_archived_ids,
                "skipped_count": len(skipped),
                "skipped": skipped,
            }
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.post("/evolution/proposals/{proposal_id}/restore")
async def restore_admin_evolution_proposal(proposal_id: str):
    """Restore one archived Proposal to the default Admin listing."""
    try:
        with _admin_app_config_context():
            proposal = SkillEvolutionService().restore_proposal(proposal_id)
            return {"success": True, "proposal": proposal.model_dump(mode="json")}
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.get("/skills/custom/{name}")
async def get_admin_custom_skill(name: str):
    """Read a custom skill and its supporting file index."""
    try:
        with _admin_app_config_context():
            name = validate_skill_name(name)
            return _custom_skill_response(name, include_content=True)
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.put("/skills/custom/{name}")
async def upsert_admin_custom_skill(name: str, req: AdminSkillUpsertRequest = Body()):
    """Create or replace a custom skill's SKILL.md."""
    try:
        with _admin_app_config_context():
            name = validate_skill_name(name)
            exists = custom_skill_exists(name)
            if not exists and public_skill_exists(name):
                raise ValueError(f"'{name}' is a built-in skill. Create a custom skill with a distinct name.")
            skill_file = get_custom_skill_file(name)
            prev_content = skill_file.read_text(encoding="utf-8") if skill_file.exists() else None
            publication = await SkillEvolutionService().publish_admin_change(
                action="edit" if exists else "create",
                name=name,
                content=req.content,
                note="Direct Admin edit",
            )
            scanner = _publication_scan_summary(publication)
            append_history(
                name,
                _history_record(
                    action="edit" if exists else "create",
                    file_path="SKILL.md",
                    prev_content=prev_content,
                    new_content=req.content,
                    scanner=scanner,
                ),
            )
            if req.enabled is not None:
                await asyncio.to_thread(_set_skill_enabled, name, req.enabled)
            reload_result = await _refresh_after_skill_change(reload=req.reload)
            payload = _custom_skill_response(name, include_content=True)
            payload["success"] = True
            payload["reload"] = reload_result
            payload["revision"] = publication["manifest"]["version"]
            payload["catalog_version"] = get_evolution_store().get_catalog_version()
            return payload
    except HTTPException:
        raise
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.delete("/skills/custom/{name}")
async def delete_admin_custom_skill(name: str):
    """Delete a custom skill directory."""
    try:
        with _admin_app_config_context():
            name = validate_skill_name(name)
            ensure_custom_skill_is_editable(name)
            prev_content = read_custom_skill_content(name)
            publication = await SkillEvolutionService().publish_admin_change(
                action="delete",
                name=name,
                note="Direct Admin deletion",
            )
            append_history(
                name,
                _history_record(
                    action="delete",
                    file_path="SKILL.md",
                    prev_content=prev_content,
                    new_content=None,
                    scanner={"decision": "allow", "reason": "Deletion requested by admin."},
                ),
            )
            reload_result = await _refresh_after_skill_change(reload=True)
            return {
                "success": True,
                "name": name,
                "reload": reload_result,
                "revision": publication["manifest"]["version"],
                "catalog_version": publication["catalog_version"],
            }
    except HTTPException:
        raise
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.get("/skills/custom/{name}/history")
async def get_admin_custom_skill_history(name: str):
    """Return sanitized custom skill change history."""
    try:
        with _admin_app_config_context():
            name = validate_skill_name(name)
            if not custom_skill_exists(name) and not read_history(name):
                raise FileNotFoundError(f"Custom skill '{name}' not found.")
            return {"name": name, "history": _sanitize_history(read_history(name))}
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.get("/skills/custom/{name}/revisions")
async def list_admin_custom_skill_revisions(name: str):
    """List immutable published revisions for a custom Skill."""
    try:
        with _admin_app_config_context():
            name = validate_skill_name(name)
            store = get_evolution_store()
            active_dir = get_custom_skill_dir(name)
            if active_dir.exists():
                store.bootstrap_active_skill(name, active_dir, actor="system")
            revisions = store.list_revisions(name)
            if not revisions:
                raise FileNotFoundError(f"No revisions found for custom skill '{name}'.")
            return {
                "name": name,
                "active_revision": store.get_active_revision(name),
                "catalog_version": store.get_catalog_version(),
                "revisions": [manifest.model_dump(mode="json") for manifest in revisions],
            }
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.get("/skills/custom/{name}/revisions/{version}")
async def get_admin_custom_skill_revision(name: str, version: int):
    """Read one immutable Skill revision."""
    try:
        with _admin_app_config_context():
            name = validate_skill_name(name)
            manifest, snapshot = get_evolution_store().load_revision(name, version)
            files = _list_revision_files(snapshot)
            content = (snapshot / "SKILL.md").read_text(encoding="utf-8") if snapshot is not None and (snapshot / "SKILL.md").is_file() else None
            return {"manifest": manifest.model_dump(mode="json"), "content": content, "files": files}
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.post("/skills/custom/{name}/rollback/{version}")
async def rollback_admin_custom_skill(name: str, version: int, req: AdminSkillRollbackRequest = Body(default=AdminSkillRollbackRequest())):
    """Publish a historical snapshot as a new immutable revision."""
    try:
        with _admin_app_config_context():
            name = validate_skill_name(name)
            previous_content = read_custom_skill_content(name) if custom_skill_exists(name) else None
            result = SkillEvolutionService().publisher.rollback(name, version, actor="admin", note=req.note)
            new_content = read_custom_skill_content(name) if custom_skill_exists(name) else None
            append_history(
                name,
                _history_record(
                    action="rollback",
                    file_path="SKILL.md",
                    prev_content=previous_content,
                    new_content=new_content,
                    scanner={"decision": "allow", "reason": f"Admin rollback to revision {version}."},
                ),
            )
            await _refresh_after_skill_change(reload=False)
            return {"success": True, **result}
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.put("/skills/custom/{name}/files/{file_path:path}")
async def write_admin_custom_skill_file(
    name: str,
    file_path: str,
    req: AdminSupportFileWriteRequest = Body(),
):
    """Write a supporting file under an allowed custom skill subdirectory."""
    try:
        with _admin_app_config_context():
            name = validate_skill_name(name)
            ensure_custom_skill_is_editable(name)
            target = _validate_support_path_or_raise(name, file_path)
            skill_dir = get_custom_skill_dir(name).resolve()
            relative = target.relative_to(skill_dir).as_posix()
            prev_content = target.read_text(encoding="utf-8") if target.exists() else None
            publication = await SkillEvolutionService().publish_admin_change(
                action="write_file",
                name=name,
                path=relative,
                content=req.content,
                note="Direct Admin support-file edit",
            )
            scanner = _publication_scan_summary(publication)
            append_history(
                name,
                _history_record(
                    action="write_file",
                    file_path=relative,
                    prev_content=prev_content,
                    new_content=req.content,
                    scanner=scanner,
                ),
            )
            reload_result = await _refresh_after_skill_change(reload=req.reload)
            return {
                "success": True,
                "name": name,
                "file_path": relative,
                "reload": reload_result,
                "revision": publication["manifest"]["version"],
                "catalog_version": publication["catalog_version"],
            }
    except HTTPException:
        raise
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.delete("/skills/custom/{name}/files/{file_path:path}")
async def delete_admin_custom_skill_file(name: str, file_path: str):
    """Delete a supporting file from a custom skill."""
    try:
        with _admin_app_config_context():
            name = validate_skill_name(name)
            ensure_custom_skill_is_editable(name)
            target = _validate_support_path_or_raise(name, file_path)
            if not target.exists() or not target.is_file():
                raise FileNotFoundError(f"Supporting file '{file_path}' not found for skill '{name}'.")
            prev_content = target.read_text(encoding="utf-8")
            relative = target.relative_to(get_custom_skill_dir(name).resolve()).as_posix()
            publication = await SkillEvolutionService().publish_admin_change(
                action="remove_file",
                name=name,
                path=relative,
                note="Direct Admin support-file deletion",
            )
            append_history(
                name,
                _history_record(
                    action="remove_file",
                    file_path=relative,
                    prev_content=prev_content,
                    new_content=None,
                    scanner={"decision": "allow", "reason": "Deletion requested by admin."},
                ),
            )
            return {
                "success": True,
                "name": name,
                "file_path": relative,
                "revision": publication["manifest"]["version"],
                "catalog_version": publication["catalog_version"],
            }
    except HTTPException:
        raise
    except Exception as exc:
        raise _safe_skill_error(exc) from exc


@router.patch("/runtime")
async def patch_admin_runtime(req: AdminRuntimePatchRequest = Body()):
    """Update allowlisted runtime fields in the config api section."""
    changes = _runtime_changes(req)
    if not changes:
        raise HTTPException(status_code=400, detail="At least one runtime field is required")

    path = _config_path()
    config_data = _load_config_data(path)
    _validate_runtime_changes(changes, config_data)
    api_config = config_data.setdefault("api", {})
    if not isinstance(api_config, dict):
        raise HTTPException(status_code=400, detail="api section must be an object")
    api_config.update(changes)
    _atomic_write_config(config_data, path=path)
    effects = _apply_runtime_settings(changes)

    reload_result = None
    if req.reload:
        manager = get_client_manager()
        ext_path = _resolve_extensions_config_path(create=False)
        reload_result = manager.reload_runtime_config(
            include_extensions=False,
            reset_clients=True,
            extensions_config_path=str(ext_path) if ext_path else None,
        )

    return {
        "success": True,
        "changed": changes,
        "effects": effects,
        "reload": reload_result,
    }


@router.post("/mcp/{name}/enable")
async def enable_admin_mcp_server(name: str):
    """Enable a configured MCP server."""
    return await _set_admin_mcp_enabled(name, True)


@router.post("/mcp/{name}/disable")
async def disable_admin_mcp_server(name: str):
    """Disable a configured MCP server."""
    return await _set_admin_mcp_enabled(name, False)


async def _set_admin_mcp_enabled(name: str, enabled: bool) -> dict[str, Any]:
    def update_and_reload() -> dict[str, Any]:
        with get_extensions_config_lock():
            path, data, servers = _mcp_servers_from_extensions(create=True)
            if name not in servers:
                raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
            server = servers[name]
            if not isinstance(server, dict):
                raise HTTPException(status_code=500, detail=f"MCP server '{name}' config must be an object")
            server["enabled"] = enabled
            _write_mcp_servers(path, data, servers)
        manager = get_client_manager()
        reload_result = manager.reload_runtime_config(
            include_extensions=True,
            reset_clients=True,
            extensions_config_path=str(path) if path else None,
        )
        return {
            "success": True,
            "name": name,
            "enabled": enabled,
            "server": _redact_value("mcp_server", server),
            "reload": reload_result,
        }

    return await asyncio.to_thread(update_and_reload)


@router.post("/mcp/{name}/test")
async def test_admin_mcp_server(name: str, req: AdminMcpTestRequest = Body(default_factory=AdminMcpTestRequest)):
    """Validate an MCP server config and run a conservative connectivity probe."""
    from app.routers.mcp import _validate_mcp_servers

    _, _, servers = _mcp_servers_from_extensions(create=False)
    if name not in servers:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    server = servers[name]
    if not isinstance(server, dict):
        raise HTTPException(status_code=500, detail=f"MCP server '{name}' config must be an object")
    _validate_mcp_servers({name: server})

    transport = server.get("type") or "stdio"
    result: dict[str, Any]
    if transport == "stdio":
        result = {
            "ok": True,
            "status": "validated",
            "message": "stdio config is valid; command was not executed by the Admin API.",
        }
    elif transport in {"sse", "http", "streamable_http"}:
        url = str(server.get("url") or "")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise HTTPException(status_code=400, detail=f"MCP server '{name}' requires an http(s) URL for probing")
        result = await _probe_remote_mcp(url, req.timeout_seconds)
    elif transport == "websocket":
        result = {
            "ok": True,
            "status": "validated",
            "message": "websocket URL shape is valid; socket was not opened by the Admin API.",
        }
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported MCP transport: {transport}")

    return {
        "success": bool(result.get("ok")),
        "name": name,
        "transport": transport,
        "result": result,
        "server": _redact_value("mcp_server", server),
    }
