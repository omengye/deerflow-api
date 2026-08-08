"""Normalize trusted ACP client MCP servers for one local ACP session."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from acp import schema

from deerflow.config.extensions_config import ExtensionsConfig, McpServerConfig

_MAX_CLIENT_MCP_SERVERS = 8
_MAX_NAME_LENGTH = 128


@dataclass(frozen=True, slots=True)
class ClientMCPBinding:
    """In-memory MCP configuration supplied by one trusted ACP client."""

    fingerprint: str
    extensions_config: ExtensionsConfig


def _checked_text(value: str, *, field: str, allow_empty: bool = False) -> str:
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ValueError(f"client MCP {field} must not be empty")
    if "\x00" in normalized or any(ord(char) < 32 for char in normalized):
        raise ValueError(f"client MCP {field} contains control characters")
    return normalized


def normalize_client_mcp_servers(
    mcp_servers: list[Any] | None,
    *,
    enabled: bool,
) -> ClientMCPBinding | None:
    """Validate ACP client MCP input and convert supported stdio servers.

    The resulting configuration is deliberately in-memory only. Enabling this
    feature allows the trusted ACP client to ask the DeerFlow daemon to launch
    local commands, so it remains opt-in.
    """

    if not mcp_servers:
        return None
    if not enabled:
        raise ValueError(
            "client mcpServers are disabled; set "
            "local_acp.accept_client_mcp_servers: true for a trusted local ACP client"
        )
    if len(mcp_servers) > _MAX_CLIENT_MCP_SERVERS:
        raise ValueError(
            f"client mcpServers exceeds the {_MAX_CLIENT_MCP_SERVERS}-server limit"
        )

    configs: dict[str, McpServerConfig] = {}
    canonical: list[dict[str, Any]] = []
    for server in mcp_servers:
        if not isinstance(server, schema.McpServerStdio):
            raise ValueError("only stdio client mcpServers are supported")

        name = _checked_text(server.name, field="server name")
        if len(name) > _MAX_NAME_LENGTH:
            raise ValueError(
                f"client MCP server name exceeds {_MAX_NAME_LENGTH} characters"
            )
        if name in configs:
            raise ValueError(f"duplicate client MCP server name: {name}")

        command = _checked_text(server.command, field=f"command for {name}")
        args = list(server.args)
        env: dict[str, str] = {}
        for item in server.env:
            env_name = _checked_text(item.name, field=f"environment name for {name}")
            if env_name in env:
                raise ValueError(
                    f"duplicate environment variable {env_name} for client MCP server {name}"
                )
            if "\x00" in item.value:
                raise ValueError(
                    f"environment value {env_name} for client MCP server {name} contains NUL"
                )
            env[env_name] = item.value

        configs[name] = McpServerConfig(
            type="stdio",
            command=command,
            args=args,
            env=env,
            tool_name_prefix=True,
        )
        canonical.append(
            {
                "name": name,
                "type": "stdio",
                "command": command,
                "args": args,
                "env": env,
            }
        )

    canonical.sort(key=lambda item: item["name"])
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ClientMCPBinding(
        fingerprint=hashlib.sha256(encoded).hexdigest(),
        extensions_config=ExtensionsConfig(mcpServers=configs),
    )
