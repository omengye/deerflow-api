"""Normalize trusted ACP client MCP servers for one local ACP session."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from acp import schema

from deerflow.config.extensions_config import ExtensionsConfig, McpServerConfig

_MAX_CLIENT_MCP_SERVERS = 8
_MAX_NAME_LENGTH = 128
_MAX_REMOTE_URL_LENGTH = 4096
_MAX_HTTP_HEADERS = 64
_MAX_HTTP_HEADER_NAME_LENGTH = 256
_MAX_HTTP_HEADER_VALUE_LENGTH = 65536
_HTTP_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


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


def _checked_remote_url(value: str, *, server_name: str) -> str:
    url = _checked_text(value, field=f"URL for {server_name}")
    if len(url) > _MAX_REMOTE_URL_LENGTH or any(char.isspace() for char in url):
        raise ValueError(f"client MCP remote URL for {server_name} is invalid or oversized")
    try:
        parsed = urlparse(url)
        valid = parsed.scheme.casefold() in {"http", "https"} and bool(parsed.hostname)
        if valid:
            _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            f"client MCP remote server {server_name} requires a valid http(s) URL"
        )
    return url


def _checked_http_headers(
    items: list[schema.HttpHeader],
    *,
    server_name: str,
) -> dict[str, str]:
    if len(items) > _MAX_HTTP_HEADERS:
        raise ValueError(
            f"client MCP remote server {server_name} exceeds the "
            f"{_MAX_HTTP_HEADERS}-header limit"
        )

    headers: dict[str, str] = {}
    seen_names: set[str] = set()
    for item in items:
        name = _checked_text(item.name, field=f"HTTP header name for {server_name}")
        if (
            len(name) > _MAX_HTTP_HEADER_NAME_LENGTH
            or _HTTP_HEADER_NAME_RE.fullmatch(name) is None
        ):
            raise ValueError(
                f"client MCP remote server {server_name} contains an invalid HTTP header name"
            )
        normalized_name = name.casefold()
        if normalized_name in seen_names:
            raise ValueError(
                f"duplicate HTTP header {name} for client MCP server {server_name}"
            )

        value = item.value.strip()
        if (
            len(value) > _MAX_HTTP_HEADER_VALUE_LENGTH
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError(
                f"HTTP header {name} for client MCP server {server_name} "
                "contains control characters or is oversized"
            )
        seen_names.add(normalized_name)
        headers[name] = value
    return headers


def normalize_client_mcp_servers(
    mcp_servers: list[Any] | None,
    *,
    enabled: bool,
) -> ClientMCPBinding | None:
    """Validate ACP client MCP input and convert supported stdio/SSE/HTTP servers.

    The resulting configuration is deliberately in-memory only. Enabling this
    feature allows the trusted ACP client to ask the DeerFlow daemon to launch
    local commands or connect to client-selected remote endpoints.
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
        if not isinstance(
            server,
            (schema.McpServerStdio, schema.SseMcpServer, schema.HttpMcpServer),
        ):
            # The ACP adapter deliberately maps every rejected client resource
            # to invalid_params via ValueError, including unsupported variants.
            raise ValueError(  # noqa: TRY004
                "only stdio, SSE, and HTTP client mcpServers are supported"
            )
        name = _checked_text(server.name, field="server name")
        if len(name) > _MAX_NAME_LENGTH:
            raise ValueError(
                f"client MCP server name exceeds {_MAX_NAME_LENGTH} characters"
            )
        if name in configs:
            raise ValueError(f"duplicate client MCP server name: {name}")

        if isinstance(server, schema.McpServerStdio):
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
        elif isinstance(server, (schema.SseMcpServer, schema.HttpMcpServer)):
            transport_type = (
                "sse" if isinstance(server, schema.SseMcpServer) else "http"
            )
            url = _checked_remote_url(server.url, server_name=name)
            headers = _checked_http_headers(list(server.headers), server_name=name)
            configs[name] = McpServerConfig(
                type=transport_type,
                url=url,
                headers=headers,
                tool_name_prefix=True,
            )
            canonical.append(
                {
                    "name": name,
                    "type": transport_type,
                    "url": url,
                    "headers": headers,
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
