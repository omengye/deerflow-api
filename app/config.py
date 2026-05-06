"""Global settings for DeerFlow API service."""
import logging
import os
import socket
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Force IPv4-only DNS resolution — this host has no working IPv6 to
# dashscope.aliyuncs.com, and httpx/openai/langchain will otherwise
# attempt AAAA records first and hang until timeout.
_getaddrinfo_orig = socket.getaddrinfo

def _getaddrinfo_ipv4(host, port, family=socket.AF_UNSPEC, *args, **kwargs):
    return _getaddrinfo_orig(host, port, socket.AF_INET, *args, **kwargs)

socket.getaddrinfo = _getaddrinfo_ipv4


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


def _env_csv(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return default
    items = [piece.strip() for piece in raw.split(",")]
    return [piece for piece in items if piece]


class Settings(BaseModel):
    """API service settings."""

    # DeerFlow config path (default to ./config.yaml so the README contract holds).
    config_path: str = Field(
        default=os.environ.get("DEER_FLOW_CONFIG_PATH", "./config.yaml"),
        description="Path to DeerFlow config.yaml",
    )

    # Server settings
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)

    # Checkpointer: memory / sqlite / none
    checkpointer_type: Literal["memory", "sqlite", "none"] = Field(default="sqlite")
    checkpointer_path: str = Field(default="./data/checkpoints.db")

    # Default model override (None = use config default)
    model_name: str | None = Field(default=None)

    # Agent settings
    thinking_enabled: bool = Field(default=True)
    subagent_enabled: bool = Field(default=_env_bool("DEER_FLOW_SUBAGENT_ENABLED", True))
    plan_mode: bool = Field(default=_env_bool("DEER_FLOW_PLAN_MODE", True))
    max_concurrent_subagents: int = Field(default=_env_int("DEER_FLOW_MAX_CONCURRENT_SUBAGENTS", 3), ge=2, le=4)

    # Data directory
    data_dir: str = Field(default="./data")

    # CORS — default to localhost dev origins; override via env.
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: _env_csv(
            "DEER_FLOW_CORS_ORIGINS",
            ["http://localhost:3000", "http://127.0.0.1:3000"],
        )
    )
    cors_allow_credentials: bool = Field(
        default_factory=lambda: _env_bool("DEER_FLOW_CORS_ALLOW_CREDENTIALS", False)
    )

    # API auth — disabled for local development unless keys are configured or
    # DEER_FLOW_AUTH_ENABLED=true is set.
    api_keys: list[str] = Field(default_factory=lambda: _env_csv("DEER_FLOW_API_KEYS", []))
    auth_enabled: bool = Field(
        default_factory=lambda: _env_bool(
            "DEER_FLOW_AUTH_ENABLED",
            bool(_env_csv("DEER_FLOW_API_KEYS", [])),
        )
    )

    # Per-request /chat timeout (seconds).
    chat_request_timeout: float = Field(
        default_factory=lambda: float(os.environ.get("DEER_FLOW_CHAT_TIMEOUT", "600"))
    )

    # Upload limits — defended at the API layer so a single multipart request
    # cannot exhaust disk or memory.  Sysadmins can raise these via env.
    max_upload_size_mb: int = Field(
        default_factory=lambda: _env_int("DEER_FLOW_MAX_UPLOAD_SIZE_MB", 25),
        ge=1,
        le=2048,
    )
    max_uploads_per_request: int = Field(
        default_factory=lambda: _env_int("DEER_FLOW_MAX_UPLOADS_PER_REQUEST", 10),
        ge=1,
        le=200,
    )
    # Empty list = allow any extension; otherwise the suffix (lowercased,
    # leading dot included or stripped) must appear in the set.
    allowed_upload_extensions: list[str] = Field(
        default_factory=lambda: _env_csv(
            "DEER_FLOW_ALLOWED_UPLOAD_EXTENSIONS",
            [],
        )
    )

    # Checkpointer fallback policy. Production should fail fast if persistent
    # state cannot be opened; memory fallback is an explicit local-development
    # escape hatch.
    allow_memory_fallback: bool = Field(
        default_factory=lambda: _env_bool("DEER_FLOW_ALLOW_MEMORY_FALLBACK", False)
    )

settings = Settings()


def ensure_data_dirs():
    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.joinpath("uploads").mkdir(parents=True, exist_ok=True)

    # Keep DeerFlow's own persistent state inside this API service by default.
    os.environ.setdefault("DEER_FLOW_HOME", str(data_dir.joinpath("deerflow").resolve()))
    Path(os.environ["DEER_FLOW_HOME"]).mkdir(parents=True, exist_ok=True)
