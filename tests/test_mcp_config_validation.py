from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routers.mcp import _validate_mcp_servers


def _stdio(*, command: str = "npx", args: list[str] | None = None, env: dict | None = None) -> dict:
    return {
        "type": "stdio",
        "command": command,
        "args": args or [],
        "env": env or {},
    }


def test_allows_default_package_launchers_and_server_arguments(monkeypatch):
    monkeypatch.delenv("DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST", raising=False)

    _validate_mcp_servers(
        {
            "npm_server": _stdio(args=["-y", "@example/mcp", "-c", "server.json"]),
            "uv_server": _stdio(command="uvx", args=["-c", "constraints.txt", "example-mcp"]),
        }
    )


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("npx", ["--call", "print('unsafe')"]),
        ("npx", ["--call=print('unsafe')"]),
        ("npx", ["-p", "some-package", "-c", "print('unsafe')"]),
        ("npx", ["--node-options=--require=payload.js", "some-package"]),
        ("uvx", ["--eval", "payload", "some-package"]),
    ],
)
def test_rejects_launcher_code_evaluation_flags(command, args, monkeypatch):
    monkeypatch.delenv("DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST", raising=False)

    with pytest.raises(HTTPException, match="execution flag") as exc_info:
        _validate_mcp_servers({"unsafe": _stdio(command=command, args=args)})

    assert exc_info.value.status_code == 400


def test_unknown_npx_option_cannot_hide_following_eval_flag(monkeypatch):
    monkeypatch.delenv("DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST", raising=False)

    with pytest.raises(HTTPException, match="execution flag"):
        _validate_mcp_servers(
            {"unsafe": _stdio(args=["--future-boolean-option", "--call", "payload"])}
        )


def test_rejects_command_outside_allowlist(monkeypatch):
    monkeypatch.delenv("DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST", raising=False)

    with pytest.raises(HTTPException, match="disallowed stdio command"):
        _validate_mcp_servers({"python_server": _stdio(command="python")})


def test_operator_can_extend_allowlist_but_eval_flags_stay_blocked(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST", "python")

    with pytest.raises(HTTPException, match="'-c'"):
        _validate_mcp_servers({"python_server": _stdio(command="python", args=["-Ic", "payload"])})


@pytest.mark.parametrize("command", [" npx", "npx ", "./npx", "npx.cmd", "npx;whoami"])
def test_rejects_non_bare_command_names(command, monkeypatch):
    monkeypatch.delenv("DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST", raising=False)

    with pytest.raises(HTTPException):
        _validate_mcp_servers({"unsafe": _stdio(command=command)})


@pytest.mark.parametrize(
    "env_name",
    [
        "PYTHONPATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "NODE_OPTIONS",
        "BASH_ENV",
    ],
)
def test_rejects_code_injecting_environment_variables(env_name, monkeypatch):
    monkeypatch.delenv("DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST", raising=False)

    with pytest.raises(HTTPException, match="code-injecting environment variable"):
        _validate_mcp_servers({"unsafe": _stdio(env={env_name: "payload"})})


def test_rejects_non_string_environment_values(monkeypatch):
    monkeypatch.delenv("DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST", raising=False)

    with pytest.raises(HTTPException, match="string keys to string values"):
        _validate_mcp_servers({"unsafe": _stdio(env={"PORT": 3000})})


@pytest.mark.parametrize(
    "config",
    [
        {"type": None, "command": "npx"},
        {"type": "stdio", "command": "npx", "tool_name_prefix": "false"},
        {"type": "http", "url": "https://example.test/mcp", "headers": {"X-Retry": 3}},
        {"type": "http", "url": "https://example.test/mcp", "oauth": "invalid"},
    ],
)
def test_rejects_mcp_config_that_would_fail_runtime_validation(config):
    with pytest.raises(HTTPException) as exc_info:
        _validate_mcp_servers({"invalid": config})

    assert exc_info.value.status_code == 400


def test_remote_transport_does_not_require_stdio_launcher():
    _validate_mcp_servers(
        {
            "remote": {
                "type": "streamable_http",
                "url": "https://mcp.example.com/api",
                "headers": {"Authorization": "$MCP_TOKEN"},
            }
        }
    )


@pytest.mark.parametrize(
    ("transport", "url"),
    [
        ("http", "ws://mcp.example.com/api"),
        ("streamable_http", "wss://mcp.example.com/api"),
        ("websocket", "https://mcp.example.com/api"),
        ("http", "http://"),
        ("http", "http://example.test:invalid/mcp"),
    ],
)
def test_remote_transport_requires_matching_url_scheme(transport, url):
    with pytest.raises(HTTPException, match="valid"):
        _validate_mcp_servers({"remote": {"type": transport, "url": url}})
