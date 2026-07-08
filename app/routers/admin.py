"""Admin-only configuration endpoints."""

from __future__ import annotations

import logging
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.dependencies import get_client_manager
from deerflow.config.app_config import AppConfig, pop_current_app_config, push_current_app_config
from deerflow.config.extensions_config import ExtensionsConfig
from deerflow.config.model_config import ModelConfig
from deerflow.skills.manager import (
    ALLOWED_SUPPORT_SUBDIRS,
    append_history,
    atomic_write,
    custom_skill_exists,
    ensure_custom_skill_is_editable,
    ensure_safe_support_path,
    get_custom_skill_dir,
    get_custom_skill_file,
    list_custom_skills,
    public_skill_exists,
    read_custom_skill_content,
    read_history,
    validate_skill_markdown_content,
    validate_skill_name,
)

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
_SECRET_KEY_PARTS = ("api_key", "secret", "token", "password", "authorization")


class AdminModelsUpdateRequest(BaseModel):
    models: list[dict[str, Any]] = Field(min_length=1)
    default_model: str | None = None
    reload: bool = True


class AdminReloadRequest(BaseModel):
    include_extensions: bool = True
    reset_clients: bool = True


class AdminSkillUpsertRequest(BaseModel):
    content: str = Field(min_length=1, max_length=200_000)
    enabled: bool | None = None
    reload: bool = True


class AdminSupportFileWriteRequest(BaseModel):
    content: str = Field(max_length=200_000)
    reload: bool = False


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
    reload: bool = True


class AdminMcpTestRequest(BaseModel):
    timeout_seconds: float = Field(default=5.0, gt=0, le=30)


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
}


def _config_path() -> Path:
    path = Path(settings.config_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


@contextmanager
def _admin_app_config_context():
    config = AppConfig.from_file(str(_config_path()))
    push_current_app_config(config)
    try:
        yield
    finally:
        pop_current_app_config()


def _load_config_data(path: Path | None = None) -> dict[str, Any]:
    config_path = path or _config_path()
    if not config_path.exists():
        raise HTTPException(status_code=404, detail=f"Config file not found: {config_path}")
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=500, detail=f"Config file is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read config file: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="Config file must contain a YAML object")
    return data


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
    path = _resolve_extensions_config_path(create=create)
    if path is None:
        return None, {"mcpServers": {}, "skills": {}}
    if not path.exists():
        if not create:
            return path, {"mcpServers": {}, "skills": {}}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"mcpServers": {}, "skills": {}}\n', encoding="utf-8")
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Extensions config is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="Extensions config must be a JSON object")
    data.setdefault("mcpServers", {})
    data.setdefault("skills", {})
    if not isinstance(data["mcpServers"], dict):
        raise HTTPException(status_code=500, detail="extensions mcpServers must be an object")
    if not isinstance(data["skills"], dict):
        raise HTTPException(status_code=500, detail="extensions skills must be an object")
    return path, data


def _write_extensions_data(path: Path | None, data: dict[str, Any]) -> None:
    if path is None:
        path = _resolve_extensions_config_path(create=True)
    if path is None:
        raise HTTPException(status_code=500, detail="Unable to resolve extensions config path")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".tmp",
        delete=False,
        dir=str(path.parent),
    ) as tmp_file:
        json.dump(data, tmp_file, indent=2, ensure_ascii=False)
        tmp_file.write("\n")
        tmp_path = Path(tmp_file.name)
    try:
        tmp_path.replace(path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to write extensions config: {exc}") from exc


def _validate_config_data(data: dict[str, Any], *, source_path: Path) -> None:
    source_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".yaml",
        delete=False,
        dir=str(source_path.parent),
    ) as tmp_file:
        yaml.safe_dump(data, tmp_file, allow_unicode=True, sort_keys=False)
        tmp_path = Path(tmp_file.name)
    try:
        AppConfig.from_file(str(tmp_path))
    finally:
        tmp_path.unlink(missing_ok=True)


def _atomic_write_config(data: dict[str, Any], *, path: Path) -> None:
    try:
        _validate_config_data(data, source_path=path)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=400, detail=f"Updated config did not validate: {exc}") from exc

    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".tmp",
        delete=False,
        dir=str(path.parent),
    ) as tmp_file:
        yaml.safe_dump(data, tmp_file, allow_unicode=True, sort_keys=False)
        tmp_path = Path(tmp_file.name)
    try:
        tmp_path.replace(path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to write config file: {exc}") from exc


def _is_secret_key(key: str | None) -> bool:
    if not key:
        return False
    normalized = key.lower().replace("-", "_")
    return normalized in _SECRET_KEY_NAMES or any(part in normalized for part in _SECRET_KEY_PARTS)


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


def _admin_skill_scan(content: str, *, executable: bool, location: str) -> dict[str, str]:
    lowered = content.lower()
    blocked_fragments = [
        "ignore previous instructions",
        "ignore all previous instructions",
        "reveal system prompt",
        "exfiltrate",
        "steal api key",
        "steal token",
    ]
    if any(fragment in lowered for fragment in blocked_fragments):
        raise HTTPException(status_code=400, detail=f"Security scan blocked {location}")
    if executable:
        executable_blocks = ["rm -rf /", "curl ", "wget ", "invoke-webrequest", "iex ", "downloadstring"]
        if any(fragment in lowered for fragment in executable_blocks):
            raise HTTPException(status_code=400, detail=f"Security scan blocked executable file {location}")
    return {"decision": "allow", "reason": "Deterministic admin checks passed."}


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
    }
    if include_content:
        payload["content"] = read_custom_skill_content(name)
        payload["files"] = _list_custom_support_files(name)
    return payload


def _set_skill_enabled(name: str, enabled: bool) -> None:
    path, data = _load_extensions_data(create=True)
    skills = data.setdefault("skills", {})
    skills[name] = {"enabled": enabled}
    _write_extensions_data(path, data)


def _skill_enabled_state(name: str, default: bool) -> bool:
    _, data = _load_extensions_data(create=False)
    raw = data.get("skills", {}).get(name)
    if isinstance(raw, dict) and isinstance(raw.get("enabled"), bool):
        return bool(raw["enabled"])
    return default


async def _refresh_after_skill_change(*, reload: bool) -> dict[str, Any] | None:
    try:
        from deerflow.agents.lead_agent.prompt import refresh_skills_system_prompt_cache_async

        await refresh_skills_system_prompt_cache_async()
    except Exception:
        logger.debug("Failed to refresh skills prompt cache after admin skill change", exc_info=True)
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
        if incoming.get("redacted") is True:
            if existing is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot preserve redacted value for new field {path or '<root>'}",
                )
            return existing
        if incoming.get("configured") is False and "redacted" in incoming:
            return None
        if incoming.get("source") == "env_ref" and "value" in incoming:
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
        app_config = AppConfig.from_file(str(path))
        skills_root = str(app_config.skills.get_skills_path())
    except Exception:
        logger.debug("Failed to load app config while building admin config response", exc_info=True)
        skills_root = None

    try:
        extensions_path = ExtensionsConfig.resolve_config_path()
        extensions_config_path = str(extensions_path) if extensions_path else None
    except Exception:
        logger.debug("Failed to resolve extensions config path", exc_info=True)
        extensions_config_path = None

    return {
        "config_path": str(path),
        "config_version": config_version,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "api": _redact_value("api", api_config),
        "models": [_redact_value("model", model) for model in raw_models if isinstance(model, dict)],
        "default_model": raw_config.get("default_model"),
        "paths": {
            "skills_root": skills_root,
            "extensions_config": extensions_config_path,
            "data_dir": api_config.get("data_dir") if isinstance(api_config, dict) else None,
        },
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
            "custom_skills_write": True,
            "runtime_write": True,
            "mcp_admin": True,
        },
    }


@router.get("/config")
async def get_admin_config():
    """Return a redacted view of the file-backed service configuration."""
    path = _config_path()
    raw_config = _load_config_data(path)
    return _admin_config_response(raw_config, path)


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
        restored_models.append(_restore_redacted_values(model, existing, path=str(name or "<unnamed-model>")))

    models = _validate_models(restored_models)
    model_names = {model.name for model in models}
    default_model = req.default_model
    if default_model is None:
        current_default = config_data.get("default_model")
        default_model = current_default if current_default in model_names else models[0].name
    if default_model not in model_names:
        raise HTTPException(status_code=400, detail=f"default_model '{default_model}' is not in models")

    config_data["models"] = [model.model_dump(exclude_none=True) for model in models]
    config_data["default_model"] = default_model
    _atomic_write_config(config_data, path=path)

    reload_result: dict[str, Any] | None = None
    if req.reload:
        try:
            manager = get_client_manager()
            reload_result = manager.reload_runtime_config(include_extensions=True, reset_clients=True)
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


@router.get("/skills/custom")
async def list_admin_custom_skills():
    """List custom skills that can be edited through the Admin API."""
    with _admin_app_config_context():
        return {"skills": [_custom_skill_response(skill.name) for skill in list_custom_skills()]}


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
            validate_skill_markdown_content(name, req.content)
            scanner = _admin_skill_scan(req.content, executable=False, location=f"{name}/SKILL.md")
            skill_file = get_custom_skill_file(name)
            prev_content = skill_file.read_text(encoding="utf-8") if skill_file.exists() else None
            atomic_write(skill_file, req.content)
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
                _set_skill_enabled(name, req.enabled)
            reload_result = await _refresh_after_skill_change(reload=req.reload)
            payload = _custom_skill_response(name, include_content=True)
            payload["success"] = True
            payload["reload"] = reload_result
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
            skill_dir = get_custom_skill_dir(name)
            prev_content = read_custom_skill_content(name)
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
            shutil.rmtree(skill_dir)
            reload_result = await _refresh_after_skill_change(reload=True)
            return {"success": True, "name": name, "reload": reload_result}
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
            executable = relative.startswith("scripts/")
            scanner = _admin_skill_scan(req.content, executable=executable, location=f"{name}/{file_path}")
            prev_content = target.read_text(encoding="utf-8") if target.exists() else None
            atomic_write(target, req.content)
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
            target.unlink()
            relative = target.relative_to(get_custom_skill_dir(name).resolve()).as_posix()
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
            return {"success": True, "name": name, "file_path": relative}
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
