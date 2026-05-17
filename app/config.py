"""Global settings for DeerFlow API service."""
import logging
import os
import socket
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Force IPv4-only DNS resolution — this host has no working IPv6 to
# dashscope.aliyuncs.com, and httpx/openai/langchain will otherwise
# attempt AAAA records first and hang until timeout.
_getaddrinfo_orig = socket.getaddrinfo

def _getaddrinfo_ipv4(host, port, family=socket.AF_UNSPEC, *args, **kwargs):
    return _getaddrinfo_orig(host, port, socket.AF_INET, *args, **kwargs)

socket.getaddrinfo = _getaddrinfo_ipv4


_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_config_file() -> Path:
    """Resolve the bootstrap config.yaml path for API service settings."""
    raw_path = os.environ.get("DEER_FLOW_CONFIG_PATH", "./config.yaml")
    path = Path(raw_path)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


_BOOTSTRAP_CONFIG_PATH = _resolve_config_file()


def _load_api_config() -> dict[str, Any]:
    """Load the top-level api section from config.yaml, if present."""
    path = _BOOTSTRAP_CONFIG_PATH
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        logger.warning("Failed to read API settings from %s; using environment/defaults", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    section = data.get("api") or data.get("api_service") or {}
    if not isinstance(section, dict):
        logger.warning("Ignoring non-object api section in %s", path)
        return {}
    return section


_API_CONFIG = _load_api_config()


def _resolve_config_scalar(value: Any) -> Any:
    """Resolve a $VAR scalar in api settings for backward-compatible secrets."""
    if isinstance(value, str) and value.startswith("$"):
        env_value = os.environ.get(value[1:])
        if env_value is None:
            raise ValueError(f"Environment variable {value[1:]} not found for API setting {value}")
        return env_value
    return value


def _raw_setting(name: str) -> Any:
    if name not in _API_CONFIG:
        return None
    return _resolve_config_scalar(_API_CONFIG[name])


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable with a conservative fallback."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """Parse an integer environment variable with a conservative fallback."""
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning(
            "Environment variable %s=%r is not a valid integer; falling back to %d",
            name, value, default,
        )
        return default


def _env_int_preferred(names: tuple[str, ...], default: int) -> int:
    for name in names:
        if os.environ.get(name) is not None:
            return _env_int(name, default)
    return default


def _env_csv(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return default
    items = [piece.strip() for piece in raw.split(",")]
    return [piece for piece in items if piece]


def _setting_str(name: str, env_name: str, default: str) -> str:
    value = _raw_setting(name)
    if value is None:
        return os.environ.get(env_name, default)
    return str(value)


def _setting_checkpointer_type() -> Literal["memory", "sqlite", "none"]:
    value = _setting_str("checkpointer_type", "DEER_FLOW_CHECKPOINTER_TYPE", "sqlite")
    if value == "memory":
        return value
    if value == "sqlite":
        return value
    if value == "none":
        return value
    logger.warning("checkpointer_type=%r is invalid; falling back to sqlite", value)
    return "sqlite"


def _setting_optional_str(name: str, env_name: str) -> str | None:
    value = _raw_setting(name)
    if value is None:
        env_value = os.environ.get(env_name)
        return env_value if env_value else None
    if value == "":
        return None
    return str(value)


def _setting_bool(name: str, env_name: str, default: bool) -> bool:
    value = _raw_setting(name)
    if value is None:
        return _env_bool(env_name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _setting_int(name: str, env_name: str, default: int) -> int:
    return _setting_int_preferred(name, (env_name,), default)


def _setting_int_preferred(name: str, env_names: tuple[str, ...], default: int) -> int:
    value = _raw_setting(name)
    if value is None:
        return _env_int_preferred(env_names, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("api.%s=%r is not a valid integer; falling back to %d", name, value, default)
        return default


def _setting_float(name: str, env_name: str, default: float) -> float:
    value = _raw_setting(name)
    if value is None:
        env_value = os.environ.get(env_name)
        if env_value is None:
            return default
        value = env_value
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not a valid float; falling back to %.1f", name, value, default)
        return default


def _setting_list(name: str, env_name: str, default: list[str]) -> list[str]:
    value = _raw_setting(name)
    if value is None:
        return _env_csv(env_name, default)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        items = [piece.strip() for piece in value.split(",")]
        return [piece for piece in items if piece]
    logger.warning("api.%s=%r is not a list/string; falling back to %r", name, value, default)
    return default


def _auth_enabled_default() -> bool:
    value = _raw_setting("auth_enabled")
    if value is not None:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if os.environ.get("DEER_FLOW_AUTH_ENABLED") is not None:
        return _env_bool("DEER_FLOW_AUTH_ENABLED", False)
    return bool(_setting_list("api_keys", "DEER_FLOW_API_KEYS", []))


class FeishuSettings(BaseModel):
    """Feishu (Lark) channel settings."""

    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""


def _load_feishu_settings() -> "FeishuSettings | None":
    raw = _API_CONFIG.get("feishu")

    def _val(key: str, env_names: list[str]) -> str:
        if isinstance(raw, dict) and key in raw:
            v = raw[key]
            if v:
                return str(_resolve_config_scalar(v))
        for env in env_names:
            v = os.environ.get(env)
            if v:
                return v
        return ""

    app_id = _val("app_id", ["FEISHU_APP_ID", "LARK_APP_ID"])
    enabled_raw = raw.get("enabled", True) if isinstance(raw, dict) else False
    enabled = enabled_raw if isinstance(enabled_raw, bool) else str(enabled_raw).lower() in {"true", "1", "yes", "on"}

    if not app_id and not enabled:
        return None

    return FeishuSettings(
        enabled=enabled,
        app_id=app_id,
        app_secret=_val("app_secret", ["FEISHU_APP_SECRET", "LARK_APP_SECRET"]),
        verification_token=_val("verification_token", ["FEISHU_VERIFICATION_TOKEN", "LARK_VERIFICATION_TOKEN"]),
    )


class Settings(BaseModel):
    """API service settings."""

    # Resolved DeerFlow config path. Keep this as the bootstrap file path so the
    # API layer and DeerFlow runtime cannot accidentally read different YAMLs.
    config_path: str = Field(
        default_factory=lambda: str(_BOOTSTRAP_CONFIG_PATH),
        description="Resolved path to DeerFlow config.yaml",
    )

    # Server settings
    host: str = Field(default_factory=lambda: _setting_str("host", "HOST", "0.0.0.0"))
    port: int = Field(default_factory=lambda: _setting_int("port", "PORT", 8000))

    # Checkpointer: memory / sqlite / none
    checkpointer_type: Literal["memory", "sqlite", "none"] = Field(default_factory=_setting_checkpointer_type)
    checkpointer_path: str = Field(default_factory=lambda: _setting_str("checkpointer_path", "DEER_FLOW_CHECKPOINTER_PATH", "./data/checkpoints.db"))

    # Default model override (None = use config default)
    model_name: str | None = Field(default_factory=lambda: _setting_optional_str("model_name", "DEER_FLOW_MODEL_NAME"))

    # Agent settings
    thinking_enabled: bool = Field(default_factory=lambda: _setting_bool("thinking_enabled", "DEER_FLOW_THINKING_ENABLED", True))
    subagent_enabled: bool = Field(default_factory=lambda: _setting_bool("subagent_enabled", "DEER_FLOW_SUBAGENT_ENABLED", True))
    plan_mode: bool = Field(default_factory=lambda: _setting_bool("plan_mode", "DEER_FLOW_PLAN_MODE", True))
    max_concurrent_subagents: int = Field(default_factory=lambda: _setting_int_preferred("max_concurrent_subagents", ("DEER_FLOW_MAX_CONCURRENT_SUBAGENTS", "MAX_CONCURRENT_SUBAGENTS"), 3), ge=2, le=4)
    recursion_limit: int = Field(default_factory=lambda: _setting_int("recursion_limit", "DEER_FLOW_RECURSION_LIMIT", 200), ge=10, le=2000)

    # Data directory
    data_dir: str = Field(default_factory=lambda: _setting_str("data_dir", "DEER_FLOW_DATA_DIR", "./data"))
    deerflow_home: str | None = Field(default_factory=lambda: _setting_optional_str("deerflow_home", "DEER_FLOW_HOME"))
    deerflow_host_base_dir: str | None = Field(default_factory=lambda: _setting_optional_str("deerflow_host_base_dir", "DEER_FLOW_HOST_BASE_DIR"))
    extensions_config_path: str | None = Field(default_factory=lambda: _setting_optional_str("extensions_config_path", "DEER_FLOW_EXTENSIONS_CONFIG_PATH"))

    # CORS — default to localhost dev origins; override via config.yaml or env.
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: _setting_list(
            "cors_allow_origins",
            "DEER_FLOW_CORS_ORIGINS",
            ["http://localhost:3000", "http://127.0.0.1:3000"],
        )
    )
    cors_allow_credentials: bool = Field(
        default_factory=lambda: _setting_bool("cors_allow_credentials", "DEER_FLOW_CORS_ALLOW_CREDENTIALS", False)
    )

    # API auth — disabled for local development unless keys are configured or
    # DEER_FLOW_AUTH_ENABLED=true is set.
    api_keys: list[str] = Field(default_factory=lambda: _setting_list("api_keys", "DEER_FLOW_API_KEYS", []))
    auth_enabled: bool = Field(default_factory=_auth_enabled_default)

    # Per-request /chat timeout (seconds).
    chat_request_timeout: float = Field(
        default_factory=lambda: _setting_float("chat_request_timeout", "DEER_FLOW_CHAT_TIMEOUT", 600.0)
    )

    # Upload limits — defended at the API layer so a single multipart request
    # cannot exhaust disk or memory.  Sysadmins can raise these via env.
    max_upload_size_mb: int = Field(
        default_factory=lambda: _setting_int("max_upload_size_mb", "DEER_FLOW_MAX_UPLOAD_SIZE_MB", 25),
        ge=1,
        le=2048,
    )
    max_uploads_per_request: int = Field(
        default_factory=lambda: _setting_int("max_uploads_per_request", "DEER_FLOW_MAX_UPLOADS_PER_REQUEST", 10),
        ge=1,
        le=200,
    )
    # Empty list = allow any extension; otherwise the suffix (lowercased,
    # leading dot included or stripped) must appear in the set.
    allowed_upload_extensions: list[str] = Field(
        default_factory=lambda: _setting_list(
            "allowed_upload_extensions",
            "DEER_FLOW_ALLOWED_UPLOAD_EXTENSIONS",
            [],
        )
    )

    # Checkpointer fallback policy. Production should fail fast if persistent
    # state cannot be opened; memory fallback is an explicit local-development
    # escape hatch.
    allow_memory_fallback: bool = Field(
        default_factory=lambda: _setting_bool("allow_memory_fallback", "DEER_FLOW_ALLOW_MEMORY_FALLBACK", False)
    )

    # Feishu (Lark) channel — optional, disabled unless configured.
    feishu: FeishuSettings | None = Field(default_factory=_load_feishu_settings)

    # Dynamic scheduled tasks. Tasks are created by agent tools and persisted
    # in SQLite so they survive API restarts.
    scheduler_enabled: bool = Field(
        default_factory=lambda: _setting_bool("scheduler_enabled", "DEER_FLOW_SCHEDULER_ENABLED", True)
    )
    scheduler_db_path: str = Field(
        default_factory=lambda: _setting_str("scheduler_db_path", "DEER_FLOW_SCHEDULER_DB_PATH", "./data/scheduled_tasks.db")
    )
    scheduler_poll_interval_seconds: float = Field(
        default_factory=lambda: _setting_float(
            "scheduler_poll_interval_seconds",
            "DEER_FLOW_SCHEDULER_POLL_INTERVAL_SECONDS",
            5.0,
        ),
        ge=0.5,
        le=3600,
    )
    scheduler_timezone: str = Field(
        default_factory=lambda: _setting_str("scheduler_timezone", "DEER_FLOW_SCHEDULER_TIMEZONE", "Asia/Shanghai")
    )

settings = Settings()


def ensure_data_dirs():
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.joinpath("uploads").mkdir(parents=True, exist_ok=True)

    # Keep DeerFlow's own persistent state inside this API service by default.
    deerflow_home = settings.deerflow_home or str(data_dir.joinpath("deerflow").resolve())
    os.environ["DEER_FLOW_HOME"] = deerflow_home
    if settings.deerflow_host_base_dir:
        os.environ["DEER_FLOW_HOST_BASE_DIR"] = settings.deerflow_host_base_dir
    if settings.extensions_config_path:
        os.environ["DEER_FLOW_EXTENSIONS_CONFIG_PATH"] = settings.extensions_config_path
    Path(os.environ["DEER_FLOW_HOME"]).mkdir(parents=True, exist_ok=True)
