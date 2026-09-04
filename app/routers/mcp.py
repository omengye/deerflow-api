"""MCP configuration endpoints."""
import asyncio
import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, ValidationError

from app.dependencies import get_client_manager
from deerflow.config.extensions_config import McpServerConfig
from deerflow.mcp.headers import illegal_header_value_reason

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])

# Conservative MCP server name: identifier-like, max 64 chars.  Prevents
# arbitrary keys (path separators, control chars) from polluting the config
# file or downstream tool routing.
_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ALLOWED_MCP_TYPES = {"stdio", "sse", "http", "streamable_http", "websocket"}
_MAX_MCP_SERVERS = 64
_MCP_STDIO_COMMAND_ALLOWLIST_ENV = "DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST"
_DEFAULT_MCP_STDIO_COMMAND_ALLOWLIST = frozenset({"npx", "uvx"})
_SHELL_METACHARS = frozenset(";|&`$<>\n\r")
_ARBITRARY_EXEC_ARGS = frozenset(
    {
        "-c",
        "--call",
        "-e",
        "--eval",
        "--print",
        "--shell",
        "--node-arg",
        "--node-options",
    }
)
_EXEC_ARGS_OUTSIDE_PACKAGE_LAUNCHERS = frozenset({"-p"})
_CLUSTERED_EXEC_LETTERS = frozenset(
    flag[1]
    for flag in _ARBITRARY_EXEC_ARGS | _EXEC_ARGS_OUTSIDE_PACKAGE_LAUNCHERS
    if len(flag) == 2 and flag.startswith("-")
)

# npx options which do not consume the next token. Unknown npx options are
# conservatively treated as value-taking so an unknown flag cannot hide a
# later --call/-c option. This list covers the common npm exec boolean surface.
_NPX_BOOLEAN_ARGS = frozenset(
    {
        "--audit",
        "--dry-run",
        "--force",
        "--foreground-scripts",
        "--fund",
        "--global",
        "--ignore-scripts",
        "--include-workspace-root",
        "--install-links",
        "--json",
        "--legacy-peer-deps",
        "--offline",
        "--prefer-offline",
        "--prefer-online",
        "--progress",
        "--strict-peer-deps",
        "--strict-ssl",
        "--timing",
        "--usage",
        "--version",
        "--versions",
        "--workspaces",
        "--yes",
        "-?",
        "-d",
        "-dd",
        "-ddd",
        "-f",
        "-g",
        "-h",
        "-l",
        "-n",
        "-q",
        "-s",
        "-v",
        "-y",
    }
)

# uvx options which consume a following value. Unknown uvx options are treated
# as booleans because uvx does not currently expose short eval flags.
_UVX_VALUE_ARGS = frozenset(
    {
        "--allow-insecure-host",
        "--build-constraints",
        "--cache-dir",
        "--color",
        "--config-file",
        "--config-setting",
        "--constraints",
        "--default-index",
        "--directory",
        "--env-file",
        "--exclude-newer",
        "--extra-index-url",
        "--find-links",
        "--fork-strategy",
        "--from",
        "--index",
        "--index-strategy",
        "--index-url",
        "--keyring-provider",
        "--link-mode",
        "--overrides",
        "--prerelease",
        "--project",
        "--python",
        "--python-platform",
        "--resolution",
        "--torch-backend",
        "--with",
        "--with-editable",
        "--with-requirements",
        "-C",
        "-P",
        "-b",
        "-c",
        "-f",
        "-i",
        "-p",
        "-w",
    }
)

_CODE_INJECTING_ENV_VARS = frozenset(
    {
        "BASH_ENV",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "ENV",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "PERL5OPT",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "RUBYOPT",
    }
)
_MAX_MCP_ARGS = 256
_MAX_MCP_ARG_LENGTH = 4096
_MAX_MCP_ENV_VARS = 128
_MAX_MCP_ENV_NAME_LENGTH = 256
_MAX_MCP_ENV_VALUE_LENGTH = 65536


def _allowed_stdio_commands() -> set[str]:
    """Return executable names allowed for API-managed stdio MCP servers."""
    allowed = set(_DEFAULT_MCP_STDIO_COMMAND_ALLOWLIST)
    raw = os.environ.get(_MCP_STDIO_COMMAND_ALLOWLIST_ENV)
    if raw:
        allowed.update(item.strip().casefold() for item in raw.split(",") if item.strip())
    return allowed


def _stdio_command_name(command: Any, *, server_name: str) -> str:
    """Validate and normalize a stdio command at the HTTP API boundary."""
    if not isinstance(command, str) or not command.strip():
        raise HTTPException(
            status_code=400,
            detail=f"MCP server {server_name!r} (stdio) requires a non-empty 'command'",
        )
    stripped = command.strip()
    if (
        stripped != command
        or "/" in stripped
        or "\\" in stripped
        or any(ch.isspace() for ch in stripped)
        or any(ch in stripped for ch in _SHELL_METACHARS)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"MCP server {server_name!r} command must be a single executable "
                "name; put parameters in 'args'"
            ),
        )
    return stripped.casefold()


def _launcher_option_region(args: list[str], *, command: str) -> list[str]:
    """Return the leading argv portion parsed by npx/uvx themselves."""
    region: list[str] = []
    index = 0
    while index < len(args):
        token = args[index].strip()
        if token in {"--", "-"} or not token.startswith("-"):
            break
        region.append(token)
        index += 1
        if "=" in token:
            continue
        if command == "npx":
            consumes_value = token not in _NPX_BOOLEAN_ARGS
        else:
            consumes_value = token in _UVX_VALUE_ARGS
        if consumes_value:
            index += 1
    return region


def _arbitrary_exec_arg(args: list[str], *, command: str) -> str | None:
    """Return an argv flag that turns the launcher into a code evaluator."""
    if command in {"npx", "uvx"}:
        candidates = _launcher_option_region(args, command=command)
        # Also inspect a contiguous prefix of option-shaped tokens. This closes
        # the ambiguity where an unknown npx boolean flag could otherwise make
        # the parser skip a following --call/-c token as if it were a value.
        for token in args:
            stripped = token.strip()
            if stripped in {"--", "-"} or not stripped.startswith("-"):
                break
            if stripped not in candidates:
                candidates.append(stripped)
        denied = _ARBITRARY_EXEC_ARGS
        if command == "uvx":
            denied = frozenset(flag for flag in denied if flag.startswith("--"))
    else:
        candidates = args
        denied = _ARBITRARY_EXEC_ARGS | _EXEC_ARGS_OUTSIDE_PACKAGE_LAUNCHERS

    for arg in candidates:
        flag = arg.split("=", 1)[0].strip()
        normalized = flag.casefold() if flag.startswith("--") else flag
        if normalized in denied:
            return normalized
        if command == "npx" and normalized.startswith("-") and not normalized.startswith("--"):
            for letter in normalized[1:]:
                if letter in {"c", "e"}:
                    return f"-{letter}"
        if command not in {"npx", "uvx"} and normalized.startswith("-") and not normalized.startswith("--"):
            for letter in normalized[1:]:
                if letter in _CLUSTERED_EXEC_LETTERS:
                    return f"-{letter}"
    return None


def _validate_stdio_server(name: str, cfg: dict[str, Any]) -> None:
    command = _stdio_command_name(cfg.get("command"), server_name=name)
    allowed_commands = _allowed_stdio_commands()
    if command not in allowed_commands:
        allowed = ", ".join(sorted(allowed_commands)) or "<none>"
        raise HTTPException(
            status_code=400,
            detail=(
                f"MCP server {name!r} uses disallowed stdio command {command!r}. "
                f"Allowed commands: {allowed}. Configure "
                f"{_MCP_STDIO_COMMAND_ALLOWLIST_ENV} to extend this list."
            ),
        )

    args = cfg.get("args", [])
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise HTTPException(status_code=400, detail=f"MCP server {name!r} 'args' must be a list of strings")
    if len(args) > _MAX_MCP_ARGS:
        raise HTTPException(status_code=400, detail=f"MCP server {name!r} has too many arguments (limit: {_MAX_MCP_ARGS})")
    for arg in args:
        if "\x00" in arg or len(arg) > _MAX_MCP_ARG_LENGTH:
            raise HTTPException(status_code=400, detail=f"MCP server {name!r} contains an invalid or oversized argument")

    exec_flag = _arbitrary_exec_arg(args, command=command)
    if exec_flag is not None:
        raise HTTPException(
            status_code=400,
            detail=f"MCP server {name!r} passes disallowed execution flag {exec_flag!r} to {command!r}",
        )

    env = cfg.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()):
        raise HTTPException(status_code=400, detail=f"MCP server {name!r} 'env' must map string keys to string values")
    if len(env) > _MAX_MCP_ENV_VARS:
        raise HTTPException(status_code=400, detail=f"MCP server {name!r} has too many environment variables (limit: {_MAX_MCP_ENV_VARS})")
    for env_name, env_value in env.items():
        if (
            not env_name
            or "=" in env_name
            or "\x00" in env_name
            or "\x00" in env_value
            or len(env_name) > _MAX_MCP_ENV_NAME_LENGTH
            or len(env_value) > _MAX_MCP_ENV_VALUE_LENGTH
        ):
            raise HTTPException(status_code=400, detail=f"MCP server {name!r} contains an invalid or oversized environment variable")
        if env_name.strip().upper() in _CODE_INJECTING_ENV_VARS:
            raise HTTPException(
                status_code=400,
                detail=f"MCP server {name!r} sets code-injecting environment variable {env_name!r}",
            )


def _validate_mcp_servers(mcp_servers: dict[str, Any]) -> None:
    """Lightweight schema check for the MCP server map.

    We do not try to mirror DeerFlow's full pydantic schema here; the goal is
    to stop obviously malformed payloads (wrong types, runaway sizes, bad
    names) before they hit the on-disk config writer.
    """
    if not isinstance(mcp_servers, dict):
        raise HTTPException(status_code=400, detail="mcp_servers must be an object")

    if len(mcp_servers) > _MAX_MCP_SERVERS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many MCP servers (limit: {_MAX_MCP_SERVERS})",
        )

    for name, cfg in mcp_servers.items():
        if not isinstance(name, str) or not _MCP_NAME_RE.match(name):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid MCP server name: {name!r}",
            )
        if not isinstance(cfg, dict):
            raise HTTPException(
                status_code=400,
                detail=f"MCP server {name!r} config must be an object",
            )

        srv_type = cfg.get("type", "stdio")
        if not isinstance(srv_type, str) or srv_type not in _ALLOWED_MCP_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"MCP server {name!r} has unsupported type {srv_type!r}; "
                    f"allowed: {sorted(_ALLOWED_MCP_TYPES)}"
                ),
            )

        # stdio servers need a command; remote servers need a url.
        if srv_type == "stdio":
            _validate_stdio_server(name, cfg)
        elif srv_type in {"sse", "http", "streamable_http", "websocket"}:
            url = cfg.get("url")
            allowed_schemes = {"ws", "wss"} if srv_type == "websocket" else {"http", "https"}
            try:
                parsed_url = urlparse(url) if isinstance(url, str) else None
                valid_url = bool(
                    parsed_url
                    and parsed_url.scheme.casefold() in allowed_schemes
                    and parsed_url.hostname
                    and not any(char.isspace() for char in url)
                )
                if valid_url:
                    _ = parsed_url.port
            except ValueError:
                valid_url = False
            if not valid_url:
                raise HTTPException(
                    status_code=400,
                    detail=f"MCP server {name!r} requires a valid {srv_type!r} URL",
                )

        for boolean_field in ("enabled", "tool_name_prefix"):
            if boolean_field in cfg and not isinstance(cfg[boolean_field], bool):
                raise HTTPException(
                    status_code=400,
                    detail=f"MCP server {name!r} {boolean_field!r} must be a boolean",
                )

        env = cfg.get("env")
        if env is not None and not (
            isinstance(env, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
        ):
            raise HTTPException(
                status_code=400,
                detail=f"MCP server {name!r} 'env' must map string keys to string values",
            )
        headers = cfg.get("headers")
        if headers is not None and not (
            isinstance(headers, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())
        ):
            raise HTTPException(
                status_code=400,
                detail=f"MCP server {name!r} 'headers' must map string keys to string values",
            )
        for header_name, header_value in (headers or {}).items():
            reason = illegal_header_value_reason(header_value)
            if reason is not None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"MCP server {name!r} header {header_name!r} cannot be sent "
                        f"as an HTTP header value because it {reason}"
                    ),
                )
        try:
            McpServerConfig.model_validate(cfg)
        except ValidationError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"MCP server {name!r} config is invalid: {exc.errors(include_url=False)}",
            ) from exc


class MCPConfigRequest(BaseModel):
    mcp_servers: dict[str, Any]


@router.get("/mcp/config")
async def get_mcp_config():
    """Get current MCP server configurations."""
    manager = get_client_manager()
    client = manager.get_client()

    try:
        result = client.get_mcp_config()
        return {"mcp_servers": result.get("mcp_servers", {})}
    except Exception:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/mcp/config")
async def update_mcp_config(req: MCPConfigRequest = Body()):
    """Update MCP server configurations."""
    _validate_mcp_servers(req.mcp_servers)

    manager = get_client_manager()
    client = manager.get_client()

    try:
        result = await asyncio.to_thread(client.update_mcp_config, req.mcp_servers)
        return {"success": True, "message": "MCP configuration updated", **result}
    except Exception:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")
