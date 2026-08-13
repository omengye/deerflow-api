"""Host-side OpenCLI tool.

The regular ``bash`` tool executes inside the configured sandbox.  This tool
is intentionally loaded by the DeerFlow API process instead, allowing a small
set of OpenCLI site adapters to reuse the host's browser bridge and login
state without exposing a general host shell.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil

from langchain_core.tools import tool

from deerflow.config import get_app_config
from deerflow.sandbox.security import HOST_TOOLS_DISABLED_MESSAGE, is_host_tool_allowed

ALLOWED_SITES = frozenset({"web", "twitter", "xiaohongshu", "xiaoyuzhou", "weixin", "douyin", "bilibili"})

_DEFAULT_TIMEOUT_SECONDS = 120
_MAX_STDOUT_CHARS = 1_000_000
_MAX_STDERR_CHARS = 100_000


def _get_opencli_executable() -> str:
    """Return the configured host OpenCLI executable.

    ``OPENCLI_BIN`` remains available as a process-level override.  The tool
    configuration is preferable for long-running ACP daemons, especially on
    Windows where ``opencli`` may resolve to a ``.cmd`` shim that
    ``create_subprocess_exec`` cannot discover by its extensionless name.
    """
    environment_override = os.environ.get("OPENCLI_BIN")
    if environment_override and environment_override.strip():
        return environment_override.strip()

    tool_config = get_app_config().get_tool_config("host_opencli")
    if tool_config is not None:
        configured = (tool_config.model_extra or {}).get("executable")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()

    # Resolve PATHEXT here instead of relying on CreateProcess to turn
    # ``opencli`` into ``opencli.cmd`` on Windows.
    return shutil.which("opencli") or shutil.which("opencli.cmd") or "opencli"


def _opencli_argv(site: str, command: str, arguments: list[str] | None) -> list[str]:
    argv = [_get_opencli_executable(), site, command, *(arguments or [])]
    if not any(arg == "-f" or arg == "--format" or arg.startswith("--format=") for arg in argv[3:]):
        argv.extend(["--format", "json"])
    return argv


async def _run_host_opencli(site: str, command: str, arguments: list[str] | None = None) -> str:
    if not is_host_tool_allowed():
        return f"Error: {HOST_TOOLS_DISABLED_MESSAGE}"
    if site not in ALLOWED_SITES:
        return f"Error: OpenCLI site is not allowed: {site}"
    if not command.strip():
        return "Error: OpenCLI command must not be empty"

    argv = _opencli_argv(site, command, arguments)
    env = os.environ.copy()
    env.setdefault("OPENCLI_BROWSER_CONNECT_TIMEOUT", "45")
    env.setdefault("OPENCLI_BROWSER_COMMAND_TIMEOUT", "90")

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        return f"Error: OpenCLI executable was not found: {argv[0]}"
    except OSError as exc:
        return f"Error: Failed to start host OpenCLI: {exc}"

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        return f"Error: Host OpenCLI command timed out after {_DEFAULT_TIMEOUT_SECONDS} seconds"

    payload = {
        "exit_code": process.returncode,
        "stdout": stdout.decode("utf-8", errors="replace")[:_MAX_STDOUT_CHARS],
        "stderr": stderr.decode("utf-8", errors="replace")[:_MAX_STDERR_CHARS],
    }
    return json.dumps(payload, ensure_ascii=False)


@tool("host_opencli", parse_docstring=True)
async def host_opencli_tool(
    description: str,
    site: str,
    command: str,
    arguments: list[str] | None = None,
) -> str:
    """Run an OpenCLI site-adapter command on the DeerFlow host.

    Args:
        description: Briefly explain why this OpenCLI command is needed.
        site: Allowed OpenCLI site adapter, such as ``twitter`` or ``github``.
        command: Command exposed by the selected OpenCLI site adapter.
        arguments: Optional positional arguments and flags passed to the command.
    """
    return await _run_host_opencli(site, command, arguments)
