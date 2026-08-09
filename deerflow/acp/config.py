"""Configuration bootstrap for the local stdio ACP process."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve_config_path(config_path: str | None) -> Path:
    """Resolve config without consulting the ACP client's process cwd."""

    raw = config_path or os.getenv("DEER_FLOW_CONFIG_PATH")
    path = Path(raw).expanduser() if raw else _PROJECT_ROOT / "config.yaml"
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"DeerFlow configuration file not found: {path}")
    return path


def _resolve_path(value: str | Path, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _value(section: dict[str, Any], key: str, fallback: Any) -> Any:
    value = section.get(key)
    return fallback if value is None else value


def _env_int(name: str, fallback: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return fallback
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_float(name: str, fallback: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return fallback
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _as_bool(value: Any, *, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class LocalACPConfig:
    """Resolved settings for one local ACP stdio process."""

    config_path: Path
    checkpointer_path: Path
    session_store_path: Path
    deerflow_home: Path | None = None
    deerflow_host_base_dir: Path | None = None
    extensions_config_path: Path | None = None
    max_active_runs: int = 2
    run_timeout_seconds: float = 600.0
    session_page_size: int = 50
    model_name: str | None = None
    thinking_enabled: bool = True
    # Kept in the resolved shape for persisted-session compatibility. Local
    # ACP is deliberately single-agent and always resolves these to False/1.
    subagent_enabled: bool = False
    plan_mode: bool = True
    max_concurrent_subagents: int = 1
    recursion_limit: int = 200
    agent_name: str | None = None
    enable_bash: bool = False
    accept_client_mcp_servers: bool = False

    @classmethod
    def from_file(cls, config_path: str | None = None) -> "LocalACPConfig":
        resolved = _resolve_config_path(config_path)
        with resolved.open(encoding="utf-8") as stream:
            raw = yaml.safe_load(stream) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"Configuration root must be an object: {resolved}")

        api = raw.get("api") or raw.get("api_service") or {}
        local = raw.get("local_acp") or {}
        if not isinstance(api, dict) or not isinstance(local, dict):
            raise ValueError("api and local_acp configuration sections must be objects")

        base_dir = resolved.parent
        data_dir = _resolve_path(str(_value(api, "data_dir", "./data")), base_dir)
        checkpointer_raw = os.getenv(
            "DEER_FLOW_ACP_CHECKPOINTER_PATH",
            str(_value(local, "checkpointer_path", data_dir / "acp-checkpoints.db")),
        )
        session_store_raw = os.getenv(
            "DEER_FLOW_ACP_SESSION_STORE_PATH",
            str(_value(local, "session_store_path", data_dir / "acp-sessions.db")),
        )

        max_active_runs = _env_int(
            "DEER_FLOW_ACP_MAX_ACTIVE_RUNS",
            int(_value(local, "max_active_runs", 2)),
        )
        run_timeout = _env_float(
            "DEER_FLOW_ACP_RUN_TIMEOUT",
            float(_value(local, "run_timeout_seconds", _value(api, "chat_request_timeout", 600.0))),
        )
        page_size = int(_value(local, "session_page_size", 50))
        recursion_limit = int(_value(local, "recursion_limit", _value(api, "recursion_limit", 200)))
        deerflow_home_raw = (
            api.get("deerflow_home")
            or os.getenv("DEER_FLOW_HOME")
            or data_dir / "deerflow"
        )
        host_base_raw = api.get("deerflow_host_base_dir") or os.getenv(
            "DEER_FLOW_HOST_BASE_DIR"
        )
        extensions_raw = api.get("extensions_config_path") or os.getenv(
            "DEER_FLOW_EXTENSIONS_CONFIG_PATH"
        )

        if max_active_runs < 1:
            raise ValueError("local_acp.max_active_runs must be at least 1")
        if run_timeout <= 0:
            raise ValueError("local_acp.run_timeout_seconds must be greater than 0")
        if not 1 <= page_size <= 500:
            raise ValueError("local_acp.session_page_size must be between 1 and 500")
        if recursion_limit < 10:
            raise ValueError("local_acp.recursion_limit must be at least 10")

        result = cls(
            config_path=resolved,
            checkpointer_path=_resolve_path(checkpointer_raw, base_dir),
            session_store_path=_resolve_path(session_store_raw, base_dir),
            deerflow_home=_resolve_path(str(deerflow_home_raw), base_dir),
            deerflow_host_base_dir=(
                _resolve_path(str(host_base_raw), base_dir) if host_base_raw else None
            ),
            extensions_config_path=(
                _resolve_path(str(extensions_raw), base_dir) if extensions_raw else None
            ),
            max_active_runs=max_active_runs,
            run_timeout_seconds=run_timeout,
            session_page_size=page_size,
            model_name=_value(local, "model_name", api.get("model_name")),
            thinking_enabled=_as_bool(
                _value(local, "thinking_enabled", _value(api, "thinking_enabled", True)),
                name="local_acp.thinking_enabled",
            ),
            subagent_enabled=False,
            plan_mode=_as_bool(
                _value(local, "plan_mode", _value(api, "plan_mode", True)),
                name="local_acp.plan_mode",
            ),
            max_concurrent_subagents=1,
            recursion_limit=recursion_limit,
            agent_name=_value(local, "agent_name", api.get("agent_name")),
            enable_bash=_as_bool(
                os.getenv(
                    "DEER_FLOW_ACP_ENABLE_BASH",
                    _value(local, "enable_bash", False),
                ),
                name="local_acp.enable_bash",
            ),
            accept_client_mcp_servers=_as_bool(
                os.getenv(
                    "DEER_FLOW_ACP_ACCEPT_CLIENT_MCP_SERVERS",
                    _value(local, "accept_client_mcp_servers", False),
                ),
                name="local_acp.accept_client_mcp_servers",
            ),
        )
        return result

    def prepare_environment(self) -> None:
        """Make path resolution independent from the ACP client's cwd."""

        os.environ["DEER_FLOW_CONFIG_PATH"] = str(self.config_path)
        if self.deerflow_home is not None:
            os.environ["DEER_FLOW_HOME"] = str(self.deerflow_home)
        if self.deerflow_host_base_dir is not None:
            os.environ["DEER_FLOW_HOST_BASE_DIR"] = str(self.deerflow_host_base_dir)
        if self.extensions_config_path is not None:
            os.environ["DEER_FLOW_EXTENSIONS_CONFIG_PATH"] = str(
                self.extensions_config_path
            )

        self.checkpointer_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_store_path.parent.mkdir(parents=True, exist_ok=True)
        if self.deerflow_home is not None:
            self.deerflow_home.mkdir(parents=True, exist_ok=True)
