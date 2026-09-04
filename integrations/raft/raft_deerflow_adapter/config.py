"""TOML and command-line configuration for the adapter."""

from __future__ import annotations

import argparse
import os
import secrets
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a TOML table")
    return value


def _strings(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be an array of strings")
    return list(value)


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    raft_command: str = "raft"
    raft_args: list[str] = field(default_factory=list)
    raft_profile: str = ""
    raft_agent_id: str = ""
    start_bridge: bool = True
    poll_interval_seconds: float = 30.0
    bridge_restart_seconds: float = 3.0
    inbox_transport_retry_attempts: int = 3
    inbox_transport_retry_base_seconds: float = 1.0
    inbox_transport_retry_max_seconds: float = 4.0
    max_message_attempts: int = 5
    deerflow_command: str = ""
    deerflow_args: list[str] = field(default_factory=list)
    workspace: Path = Path.cwd()
    acp_timeout_seconds: float = 600.0
    state_path: Path = Path("data/raft-deerflow-adapter.sqlite3")
    wake_host: str = "127.0.0.1"
    wake_port: int = 47821
    wake_token: str = ""
    runtime_session: str = "deerflow-acp"
    log_level: str = "INFO"

    @classmethod
    def from_toml(cls, path: Path) -> "AdapterConfig":
        resolved = path.expanduser().resolve()
        with resolved.open("rb") as handle:
            data = tomllib.load(handle)
        base = resolved.parent
        raft = _table(data, "raft")
        deerflow = _table(data, "deerflow")
        state = _table(data, "state")
        wake = _table(data, "wake")
        adapter = _table(data, "adapter")

        workspace = Path(deerflow.get("workspace", "."))
        if not workspace.is_absolute():
            workspace = base / workspace
        state_path = Path(state.get("path", "data/raft-deerflow-adapter.sqlite3"))
        if not state_path.is_absolute():
            state_path = base / state_path

        return cls(
            raft_command=str(raft.get("command", "raft")),
            raft_args=_strings(raft.get("args"), field_name="raft.args"),
            raft_profile=str(raft.get("profile", os.getenv("RAFT_PROFILE", ""))),
            raft_agent_id=str(
                raft.get("agent_id", os.getenv("RAFT_EXPECTED_AGENT_ID", ""))
            ),
            start_bridge=bool(raft.get("start_bridge", True)),
            poll_interval_seconds=float(adapter.get("poll_interval_seconds", 30)),
            bridge_restart_seconds=float(adapter.get("bridge_restart_seconds", 3)),
            inbox_transport_retry_attempts=int(
                adapter.get("inbox_transport_retry_attempts", 3)
            ),
            inbox_transport_retry_base_seconds=float(
                adapter.get("inbox_transport_retry_base_seconds", 1)
            ),
            inbox_transport_retry_max_seconds=float(
                adapter.get("inbox_transport_retry_max_seconds", 4)
            ),
            max_message_attempts=int(adapter.get("max_message_attempts", 5)),
            deerflow_command=str(deerflow.get("command", "")),
            deerflow_args=_strings(
                deerflow.get("args"), field_name="deerflow.args"
            ),
            workspace=workspace.resolve(),
            acp_timeout_seconds=float(deerflow.get("timeout_seconds", 600)),
            state_path=state_path.resolve(),
            wake_host=str(wake.get("host", "127.0.0.1")),
            wake_port=int(wake.get("port", 47821)),
            wake_token=str(wake.get("token", "")),
            runtime_session=str(wake.get("runtime_session", "deerflow-acp")),
            log_level=str(adapter.get("log_level", "INFO")),
        ).validated()

    def with_runtime_defaults(self) -> "AdapterConfig":
        token = self.wake_token or secrets.token_urlsafe(32)
        return replace(self, wake_token=token)

    def validated(self) -> "AdapterConfig":
        if not self.raft_command.strip():
            raise ValueError("raft.command must not be empty")
        if not self.raft_profile.strip():
            raise ValueError("raft.profile or RAFT_PROFILE is required")
        if self.start_bridge and not self.raft_agent_id.strip():
            raise ValueError(
                "raft.agent_id or RAFT_EXPECTED_AGENT_ID is required when start_bridge=true"
            )
        if not self.deerflow_command.strip():
            raise ValueError("deerflow.command must point to deerflow-acp.exe")
        if not self.workspace.exists() or not self.workspace.is_dir():
            raise ValueError(f"deerflow.workspace must be an existing directory: {self.workspace}")
        if self.poll_interval_seconds <= 0:
            raise ValueError("adapter.poll_interval_seconds must be positive")
        if self.bridge_restart_seconds <= 0:
            raise ValueError("adapter.bridge_restart_seconds must be positive")
        if self.inbox_transport_retry_attempts <= 0:
            raise ValueError(
                "adapter.inbox_transport_retry_attempts must be positive"
            )
        if self.inbox_transport_retry_base_seconds < 0:
            raise ValueError(
                "adapter.inbox_transport_retry_base_seconds must not be negative"
            )
        if (
            self.inbox_transport_retry_max_seconds
            < self.inbox_transport_retry_base_seconds
        ):
            raise ValueError(
                "adapter.inbox_transport_retry_max_seconds must be greater than "
                "or equal to adapter.inbox_transport_retry_base_seconds"
            )
        if self.max_message_attempts <= 0:
            raise ValueError("adapter.max_message_attempts must be positive")
        if self.acp_timeout_seconds <= 0:
            raise ValueError("deerflow.timeout_seconds must be positive")
        if not 0 <= self.wake_port <= 65535:
            raise ValueError("wake.port must be between 0 and 65535")
        if not self.runtime_session.strip():
            raise ValueError("wake.runtime_session must not be empty")
        return self


def parse_args(argv: list[str] | None = None) -> tuple[AdapterConfig, bool]:
    parser = argparse.ArgumentParser(
        description="Connect a Raft External Agent to DeerFlow Portable ACP"
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Drain Raft once without starting the long-lived wake bridge",
    )
    args = parser.parse_args(argv)
    config = AdapterConfig.from_toml(args.config).with_runtime_defaults()
    return config, bool(args.once)
