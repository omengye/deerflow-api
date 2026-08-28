"""Built-in tool for invoking external ACP-compatible agents."""

import asyncio
import logging
import os
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, cast

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolArg, StructuredTool
from pydantic import BaseModel, Field

from .acp_artifact_downloader import ACPArtifactDownloader, DownloadedACPArtifact

logger = logging.getLogger(__name__)

_ACP_LIVE_BATCH_DELAY_SECONDS = 0.04
_ACP_LIVE_BATCH_MAX_CHARS = 128
_LiveEventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class _ACPProgressEmitter:
    """Forward ACP chunks to the live run stream without flooding its backend."""

    def __init__(
        self,
        callback: _LiveEventCallback | None,
        *,
        task_id: str,
        agent: str,
    ) -> None:
        self._callback = callback
        self._task_id = task_id
        self._agent = agent
        self._queue: asyncio.Queue[tuple[str, str] | None] | None = (
            asyncio.Queue() if callback is not None else None
        )
        self._worker: asyncio.Task[None] | None = None
        self._finished = False

    async def start(self) -> None:
        if self._callback is None or self._worker is not None:
            return
        self._worker = asyncio.create_task(
            self._run(),
            name=f"acp-progress-{self._task_id}",
        )
        await self._emit(
            {
                "type": "subagent_started",
                "task_id": self._task_id,
                "name": self._agent,
                "description": f"ACP agent: {self._agent}",
            }
        )

    def add_text(self, event_type: str, text: str) -> None:
        queue = self._queue
        if queue is None or self._finished or not text:
            return
        queue.put_nowait((event_type, text))

    async def finish(self, event_type: str, **details: Any) -> None:
        if self._finished:
            return
        self._finished = True
        queue = self._queue
        if queue is not None:
            queue.put_nowait(None)
        if self._worker is not None:
            await self._worker
            self._worker = None
        if self._callback is not None:
            await self._emit(
                {
                    "type": event_type,
                    "task_id": self._task_id,
                    "agent": self._agent,
                    **details,
                }
            )

    async def _run(self) -> None:
        queue = self._queue
        if queue is None:
            return
        loop = asyncio.get_running_loop()
        stopped = False
        while not stopped:
            item = await queue.get()
            if item is None:
                return

            event_type, text = item
            batch: list[tuple[str, str]] = [(event_type, text)]
            batch_chars = len(text)
            deadline = loop.time() + _ACP_LIVE_BATCH_DELAY_SECONDS
            while batch_chars < _ACP_LIVE_BATCH_MAX_CHARS:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError:
                    break
                if item is None:
                    stopped = True
                    break
                event_type, text = item
                batch_chars += len(text)
                if batch[-1][0] == event_type:
                    previous_type, previous_text = batch[-1]
                    batch[-1] = (previous_type, previous_text + text)
                else:
                    batch.append((event_type, text))

            for event_type, text in batch:
                field = "thinking" if event_type == "thinking_chunk" else "content"
                await self._emit(
                    {
                        "type": event_type,
                        "task_id": self._task_id,
                        "agent": self._agent,
                        field: text,
                    }
                )

    async def _emit(self, event: dict[str, Any]) -> None:
        callback = self._callback
        if callback is None:
            return
        try:
            await callback(event)
        except Exception:
            logger.warning(
                "Failed to publish live ACP progress for agent %s",
                self._agent,
                exc_info=True,
            )


class _InvokeACPAgentInput(BaseModel):
    agent: str = Field(description="Name of the ACP agent to invoke")
    prompt: str = Field(description="The concise task prompt to send to the agent")


def _get_work_dir(thread_id: str | None) -> str:
    """Get the per-thread ACP workspace directory.

    Each thread gets an isolated workspace under
    ``{base_dir}/threads/{thread_id}/acp-workspace/`` so that concurrent
    sessions cannot read or overwrite each other's ACP agent outputs.

    Falls back to the legacy global ``{base_dir}/acp-workspace/`` when
    ``thread_id`` is not available (e.g. embedded / direct invocation).

    The directory is created automatically if it does not exist.

    Returns:
        An absolute physical filesystem path to use as the working directory.
    """
    from deerflow.config.paths import get_paths

    paths = get_paths()
    if thread_id:
        try:
            work_dir = paths.acp_workspace_dir(thread_id)
        except ValueError:
            logger.warning(
                "Invalid thread_id %r for ACP workspace, falling back to global",
                thread_id,
            )
            work_dir = paths.base_dir / "acp-workspace"
    else:
        work_dir = paths.base_dir / "acp-workspace"

    work_dir.mkdir(parents=True, exist_ok=True)
    logger.info("ACP agent work_dir: %s", work_dir)
    return str(work_dir)


def _build_mcp_servers() -> dict[str, dict[str, Any]]:
    """Build ACP ``mcpServers`` config from DeerFlow's enabled MCP servers."""
    from deerflow.config.extensions_config import ExtensionsConfig
    from deerflow.mcp.client import build_servers_config

    return build_servers_config(ExtensionsConfig.from_file())


def _build_acp_mcp_servers() -> list[dict[str, Any]]:
    """Build ACP ``mcpServers`` payload for ``new_session``.

    The ACP client expects a list of server objects, while DeerFlow's MCP helper
    returns a name -> config mapping for the LangChain MCP adapter. This helper
    converts the enabled servers into the ACP wire format.
    """
    from deerflow.config.extensions_config import ExtensionsConfig

    extensions_config = ExtensionsConfig.from_file()
    enabled_servers = extensions_config.get_enabled_mcp_servers()

    mcp_servers: list[dict[str, Any]] = []
    for name, server_config in enabled_servers.items():
        transport_type = server_config.type or "stdio"
        payload: dict[str, Any] = {"name": name, "type": transport_type}

        if transport_type == "stdio":
            if not server_config.command:
                raise ValueError(
                    f"MCP server '{name}' with stdio transport requires 'command' field"
                )
            payload["command"] = server_config.command
            payload["args"] = server_config.args
            payload["env"] = [
                {"name": key, "value": value}
                for key, value in server_config.env.items()
            ]
        elif transport_type in ("http", "sse"):
            if not server_config.url:
                raise ValueError(
                    f"MCP server '{name}' with {transport_type} transport requires 'url' field"
                )
            payload["url"] = server_config.url
            payload["headers"] = [
                {"name": key, "value": value}
                for key, value in server_config.headers.items()
            ]
        else:
            raise ValueError(
                f"MCP server '{name}' has unsupported transport type: {transport_type}"
            )

        mcp_servers.append(payload)

    return mcp_servers


def _build_permission_response(options: list[Any], *, auto_approve: bool) -> Any:
    """Build an ACP permission response.

    When ``auto_approve`` is True, selects the first ``allow_once`` (preferred)
    or ``allow_always`` option.  When False (the default), always cancels —
    permission requests must be handled by the ACP agent's own policy or the
    agent must be configured to operate without requesting permissions.
    """
    from acp import RequestPermissionResponse
    from acp.schema import AllowedOutcome, DeniedOutcome

    if auto_approve:
        for preferred_kind in ("allow_once", "allow_always"):
            for option in options:
                if getattr(option, "kind", None) != preferred_kind:
                    continue

                option_id = getattr(option, "option_id", None)
                if option_id is None:
                    option_id = getattr(option, "optionId", None)
                if option_id is None:
                    continue

                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", optionId=option_id),
                )

    return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))


def _format_invocation_error(agent: str, cmd: str, exc: Exception) -> str:
    """Return a user-facing ACP invocation error with actionable remediation."""
    if not isinstance(exc, FileNotFoundError):
        return f"Error invoking ACP agent '{agent}': {exc}"

    message = (
        f"Error invoking ACP agent '{agent}': Command '{cmd}' was not found on PATH."
    )
    if cmd == "codex-acp" and shutil.which("codex"):
        return f"{message} The installed `codex` CLI does not speak ACP directly. Install a Codex ACP adapter (for example `npx @zed-industries/codex-acp`) or update `acp_agents.codex.command` and `args` in config.yaml."

    return f"{message} Install the agent binary or update `acp_agents.{agent}.command` in config.yaml."


def build_invoke_acp_agent_tool(agents: dict) -> BaseTool:
    """Create the ``invoke_acp_agent`` tool with a description generated from configured agents.

    The tool description includes the list of available agents so that the LLM
    knows which agents it can invoke without requiring hardcoded names.

    Args:
        agents: Mapping of agent name -> ``ACPAgentConfig``.

    Returns:
        A LangChain ``BaseTool`` ready to be included in the tool list.
    """
    agent_lines = "\n".join(
        f"- {name}: {cfg.description}" for name, cfg in agents.items()
    )
    description = (
        "Invoke an external ACP-compatible agent and return its final response.\n\n"
        "Available agents:\n"
        f"{agent_lines}\n\n"
        "IMPORTANT: ACP agents operate in their own independent workspace. "
        "Do NOT include /mnt/user-data paths in the prompt. "
        "Give the agent a self-contained task description — it will produce results in its own workspace. "
        "ACP resource links are downloaded into /mnt/acp-workspace/ (read-only) before this tool returns."
    )

    # Capture agents in closure so the function can reference it
    _agents = dict(agents)
    _agent_locks = {name: asyncio.Lock() for name in _agents}

    async def _invoke_unlocked(
        agent: str,
        prompt: str,
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
    ) -> str:
        logger.info("Invoking ACP agent %s (prompt length: %d)", agent, len(prompt))
        logger.debug(
            "Invoking ACP agent %s with prompt: %.200s%s",
            agent,
            prompt,
            "..." if len(prompt) > 200 else "",
        )
        if agent not in _agents:
            available = ", ".join(_agents.keys())
            return f"Error: Unknown agent '{agent}'. Available: {available}"

        agent_config = _agents[agent]
        thread_id: str | None = ((config or {}).get("configurable") or {}).get(
            "thread_id"
        )
        physical_cwd = await asyncio.to_thread(_get_work_dir, thread_id)
        artifact_downloader = ACPArtifactDownloader(
            Path(physical_cwd),
            uuid.uuid4().hex,
            allowed_hosts=agent_config.artifact_allowed_hosts,
            allow_insecure_http=agent_config.artifact_allow_insecure_http,
            max_bytes=agent_config.artifact_max_file_size_mb * 1024 * 1024,
            timeout_seconds=agent_config.artifact_download_timeout_seconds,
        )

        try:
            from acp import PROTOCOL_VERSION, Client, text_block
            from acp.schema import (
                AgentMessageChunk,
                AgentThoughtChunk,
                ClientCapabilities,
                Implementation,
                ResourceContentBlock,
                TextContentBlock,
            )
        except ImportError:
            return "Error: agent-client-protocol package is not installed. Run `uv sync` to install project dependencies."

        metadata = (config or {}).get("metadata") or {}
        callback_candidate = metadata.get("live_event_callback")
        live_callback = (
            cast(_LiveEventCallback, callback_candidate)
            if callable(callback_candidate)
            else None
        )
        invocation_id = f"acp:{agent}:{uuid.uuid4().hex}"
        progress = _ACPProgressEmitter(
            live_callback,
            task_id=invocation_id,
            agent=agent,
        )
        invocation_started_at = time.monotonic()
        await progress.start()

        class _CollectingClient(Client):
            """Minimal ACP Client that collects streamed text from session updates."""

            def __init__(self) -> None:
                self._chunks: list[str] = []
                self.artifacts: list[DownloadedACPArtifact] = []
                self.artifact_errors: list[str] = []
                self.first_update_at: float | None = None
                self.chunk_count = 0
                self._artifact_tasks: list[asyncio.Task[None]] = []

            @property
            def collected_text(self) -> str:
                return "".join(self._chunks)

            def _record_text(self, event_type: str, text: str) -> None:
                if not text:
                    return
                if self.first_update_at is None:
                    self.first_update_at = time.monotonic()
                self.chunk_count += 1
                # Preserve the existing final tool-result behaviour while also
                # forwarding the correctly typed live event.
                self._chunks.append(text)
                progress.add_text(event_type, text)

            async def _download_artifact(self, resource: ResourceContentBlock) -> None:
                try:
                    self.artifacts.append(await artifact_downloader.download(resource))
                except Exception as exc:  # noqa: BLE001 - isolate downloader failures from ACP notifications
                    self.artifact_errors.append(
                        f"{resource.name}: {type(exc).__name__}: {exc}"
                    )

            async def wait_for_artifacts(self) -> None:
                if self._artifact_tasks:
                    await asyncio.gather(*self._artifact_tasks)
                    self._artifact_tasks.clear()

            async def cancel_artifacts(self) -> None:
                tasks = list(self._artifact_tasks)
                self._artifact_tasks.clear()
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

            async def session_update(self, session_id: str, update, **kwargs) -> None:  # type: ignore[override]
                del session_id, kwargs
                try:
                    content = getattr(update, "content", None)
                    if isinstance(update, AgentMessageChunk):
                        if isinstance(content, TextContentBlock):
                            self._record_text("token_chunk", content.text)
                        elif isinstance(content, ResourceContentBlock):
                            self._artifact_tasks.append(
                                asyncio.create_task(
                                    self._download_artifact(content),
                                    name=f"acp-artifact-{invocation_id}",
                                )
                            )
                    elif isinstance(update, AgentThoughtChunk) and isinstance(
                        content, TextContentBlock
                    ):
                        self._record_text("thinking_chunk", content.text)
                except Exception as exc:  # noqa: BLE001 - malformed client updates must not break the stream
                    logger.warning("Failed to process ACP session update: %s", exc)

            async def request_permission(
                self, options, session_id: str, tool_call, **kwargs
            ):  # type: ignore[override]
                response = _build_permission_response(
                    options, auto_approve=agent_config.auto_approve_permissions
                )
                outcome = response.outcome.outcome
                if outcome == "selected":
                    logger.info(
                        "ACP permission auto-approved for tool call %s in session %s",
                        tool_call.tool_call_id,
                        session_id,
                    )
                else:
                    logger.warning(
                        "ACP permission denied for tool call %s in session %s (set auto_approve_permissions: true in config.yaml to enable)",
                        tool_call.tool_call_id,
                        session_id,
                    )
                return response

        client = _CollectingClient()
        prompt_started_at: float | None = None
        prompt_completed_at: float | None = None

        def _elapsed_ms(start: float | None, end: float | None) -> float | None:
            if start is None or end is None:
                return None
            return max(0.0, (end - start) * 1000)

        def _log_stream_metrics(outcome: str) -> None:
            now = time.monotonic()
            first_update_ms = _elapsed_ms(
                invocation_started_at,
                client.first_update_at,
            )
            prompt_total_ms = _elapsed_ms(
                prompt_started_at,
                prompt_completed_at or (now if prompt_started_at is not None else None),
            )
            finalization_ms = _elapsed_ms(prompt_completed_at, now)
            logger.info(
                "ACP agent '%s' stream metrics: outcome=%s first_update_ms=%s "
                "chunk_count=%d prompt_total_ms=%s finalization_ms=%s tool_total_ms=%.1f",
                agent,
                outcome,
                f"{first_update_ms:.1f}" if first_update_ms is not None else "none",
                client.chunk_count,
                f"{prompt_total_ms:.1f}" if prompt_total_ms is not None else "none",
                f"{finalization_ms:.1f}" if finalization_ms is not None else "none",
                (now - invocation_started_at) * 1000,
            )

        cmd = agent_config.command
        args = agent_config.args or []
        try:
            mcp_servers = await asyncio.to_thread(_build_acp_mcp_servers)
        except ValueError as exc:
            logger.warning(
                "Invalid MCP server configuration for ACP agent '%s'; continuing without MCP servers: %s",
                agent,
                exc,
            )
            mcp_servers = []
        agent_env: dict[str, str] | None = None
        if agent_config.env:
            agent_env = {
                k: (os.environ.get(v[1:], "") if v.startswith("$") else v)
                for k, v in agent_config.env.items()
            }

        try:
            from acp import spawn_agent_process

            async with spawn_agent_process(
                client, cmd, *args, env=agent_env, cwd=physical_cwd
            ) as (conn, _proc):
                logger.info(
                    "Spawning ACP agent '%s' with command '%s' and args %s in cwd %s",
                    agent,
                    cmd,
                    args,
                    physical_cwd,
                )
                async with asyncio.timeout(agent_config.timeout_seconds):
                    await conn.initialize(
                        protocol_version=PROTOCOL_VERSION,
                        client_capabilities=ClientCapabilities(),
                        client_info=Implementation(
                            name="deerflow", title="DeerFlow", version="0.1.0"
                        ),
                    )
                    session_kwargs: dict[str, Any] = {
                        "cwd": physical_cwd,
                        "mcp_servers": mcp_servers,
                    }
                    if agent_config.model:
                        session_kwargs["model"] = agent_config.model
                    session = await conn.new_session(**session_kwargs)
                    prompt_started_at = time.monotonic()
                    await conn.prompt(
                        session_id=session.session_id,
                        prompt=[text_block(prompt)],
                    )
                    prompt_completed_at = time.monotonic()
                    # The Python ACP SDK dispatches notifications in background
                    # tasks. Yield once so notifications received before the
                    # prompt response can finish their non-blocking collectors.
                    await asyncio.sleep(0)
            await client.wait_for_artifacts()
            result = client.collected_text
            if client.artifact_errors:
                details = "; ".join(client.artifact_errors)
                message = f"Error invoking ACP agent '{agent}': artifact download failed: {details}"
                await progress.finish("task_failed", error=message)
                _log_stream_metrics("artifact_failed")
                return message
            if client.artifacts:
                artifact_lines = [
                    f"- {item.virtual_path} ({item.size} bytes, sha256={item.sha256})"
                    for item in client.artifacts
                ]
                result = (
                    result.rstrip()
                    + "\n\nACP artifacts downloaded:\n"
                    + "\n".join(artifact_lines)
                ).lstrip()
            await progress.finish("task_completed")
            _log_stream_metrics("completed")
            logger.info("ACP agent '%s' returned %s", agent, result[:1000])
            logger.info("ACP agent '%s' returned %d characters", agent, len(result))
            return result or "(no response)"
        except asyncio.CancelledError:
            await client.cancel_artifacts()
            await progress.finish("task_cancelled", error="ACP invocation cancelled")
            _log_stream_metrics("cancelled")
            raise
        except TimeoutError:
            await client.cancel_artifacts()
            logger.error(
                "ACP agent '%s' invocation timed out after %s seconds",
                agent,
                agent_config.timeout_seconds,
            )
            message = (
                f"Error invoking ACP agent '{agent}': timed out after {agent_config.timeout_seconds} seconds. "
                f"Increase `acp_agents.{agent}.timeout_seconds` in config.yaml if the agent needs more time."
            )
            await progress.finish("task_timed_out", error=message)
            _log_stream_metrics("timed_out")
            return message
        except Exception as e:  # noqa: BLE001 - return external agent failures as tool results
            await client.cancel_artifacts()
            logger.error("ACP agent '%s' invocation failed: %s", agent, e)
            message = await asyncio.to_thread(_format_invocation_error, agent, cmd, e)
            await progress.finish("task_failed", error=message)
            _log_stream_metrics("failed")
            return message

    async def _invoke(
        agent: str,
        prompt: str,
        config: Annotated[RunnableConfig, InjectedToolArg] = None,
    ) -> str:
        lock = _agent_locks.get(agent)
        if lock is None:
            return await _invoke_unlocked(agent, prompt, config)
        async with lock:
            return await _invoke_unlocked(agent, prompt, config)

    return StructuredTool.from_function(
        name="invoke_acp_agent",
        description=description,
        coroutine=_invoke,
        args_schema=_InvokeACPAgentInput,
    )
