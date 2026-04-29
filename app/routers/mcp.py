"""MCP configuration endpoints."""
import logging
import re
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from app.dependencies import get_client_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])

# Conservative MCP server name: identifier-like, max 64 chars.  Prevents
# arbitrary keys (path separators, control chars) from polluting the config
# file or downstream tool routing.
_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ALLOWED_MCP_TYPES = {"stdio", "sse", "http", "streamable_http", "websocket"}
_MAX_MCP_SERVERS = 64


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

        srv_type = cfg.get("type")
        if srv_type is not None and srv_type not in _ALLOWED_MCP_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"MCP server {name!r} has unsupported type {srv_type!r}; "
                    f"allowed: {sorted(_ALLOWED_MCP_TYPES)}"
                ),
            )

        # stdio servers need a command; remote servers need a url.
        if srv_type == "stdio":
            if not isinstance(cfg.get("command"), str) or not cfg.get("command"):
                raise HTTPException(
                    status_code=400,
                    detail=f"MCP server {name!r} (stdio) requires a non-empty 'command'",
                )
            args = cfg.get("args")
            if args is not None and not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
                raise HTTPException(
                    status_code=400,
                    detail=f"MCP server {name!r} 'args' must be a list of strings",
                )
        elif srv_type in {"sse", "http", "streamable_http", "websocket"}:
            url = cfg.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://", "ws://", "wss://")):
                raise HTTPException(
                    status_code=400,
                    detail=f"MCP server {name!r} requires a valid 'url' (http(s)/ws(s))",
                )

        env = cfg.get("env")
        if env is not None and not (isinstance(env, dict) and all(isinstance(k, str) for k in env)):
            raise HTTPException(
                status_code=400,
                detail=f"MCP server {name!r} 'env' must be an object of string keys",
            )


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
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/mcp/config")
async def update_mcp_config(req: MCPConfigRequest = Body()):
    """Update MCP server configurations."""
    _validate_mcp_servers(req.mcp_servers)

    manager = get_client_manager()
    client = manager.get_client()

    try:
        result = client.update_mcp_config(req.mcp_servers)
        return {"success": True, "message": "MCP configuration updated", **result}
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")
