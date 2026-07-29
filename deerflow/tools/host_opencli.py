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

from langchain_core.tools import tool

ALLOWED_SITES = frozenset({"twitter", "github"})

_DEFAULT_TIMEOUT_SECONDS = 120
_MAX_STDOUT_CHARS = 1_000_000
_MAX_STDERR_CHARS = 100_000


def _opencli_argv(site: str, command: str, args: list[str] | None) -> list[str]:
    argv = [os.environ.get("OPENCLI_BIN", "opencli"), site, command, *(args or [])]
    if not any(arg == "-f" or arg == "--format" or arg.startswith("--format=") for arg in argv[3:]):
        argv.extend(["--format", "json"])
    return argv


async def _run_host_opencli(site: str, command: str, args: list[str] | None = None) -> str:
    if site not in ALLOWED_SITES:
        return f"Error: OpenCLI site is not allowed: {site}"
    if not command.strip():
        return "Error: OpenCLI command must not be empty"

    argv = _opencli_argv(site, command, args)
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
    args: list[str] | None = None,
) -> str:
    """Run an OpenCLI site-adapter command on the DeerFlow host.

    Args:
        description: Briefly explain why this OpenCLI command is needed.
        site: Allowed OpenCLI site adapter, such as ``twitter`` or ``github``.
        command: Command exposed by the selected OpenCLI site adapter.
        args: Optional positional arguments and flags passed to the command.
    """
    return await _run_host_opencli(site, command, args)
