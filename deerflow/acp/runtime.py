"""Embedded DeerFlow runtime used by the local ACP adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from deerflow.client import DeerFlowClient, StreamEvent

from .client_mcp import ClientMCPBinding
from .config import LocalACPConfig
from .session_store import LocalACPSession

LiveEventCallback = Callable[[dict[str, Any]], Awaitable[None]]

_LOCAL_ACP_EXCLUDED_TOOLS = {
    "bash",
    "invoke_acp_agent",
    "task",
    "task_status",
}


class LocalACPRuntime:
    """Owns the ACP-only checkpointer and embedded DeerFlow clients."""

    def __init__(self, config: LocalACPConfig):
        self.config = config
        self._checkpointer_cm: AbstractAsyncContextManager[Any] | None = None
        self._checkpointer: Any = None
        self._clients: dict[tuple[Any, ...], DeerFlowClient] = {}
        self._client_mcp_bindings: dict[str, ClientMCPBinding] = {}
        self._client_lock = asyncio.Lock()
        self._run_slots = asyncio.Semaphore(config.max_active_runs)

    async def open(self) -> None:
        if self._checkpointer is not None:
            return
        cm = AsyncSqliteSaver.from_conn_string(str(self.config.checkpointer_path))
        saver = await cm.__aenter__()
        try:
            await saver.setup()
        except BaseException:
            await cm.__aexit__(None, None, None)
            raise
        self._checkpointer_cm = cm
        self._checkpointer = saver

    async def close(self) -> None:
        client_mcp_sessions = list(self._client_mcp_bindings)
        for session_id in client_mcp_sessions:
            await self.release_client_mcp(session_id)
        self._clients.clear()
        cm = self._checkpointer_cm
        self._checkpointer_cm = None
        self._checkpointer = None
        if cm is not None:
            await cm.__aexit__(None, None, None)

    async def warmup(self) -> None:
        """Build and cache the default DeerFlow client graph without calling a model."""

        session = LocalACPSession(
            session_id="__deerflow_acp_warmup__",
            cwd="",
            title=None,
            updated_at="",
            model_name=self.config.model_name,
            thinking_enabled=self.config.thinking_enabled,
            subagent_enabled=False,
            plan_mode=self.config.plan_mode,
            max_concurrent_subagents=1,
            recursion_limit=self.config.recursion_limit,
            agent_name=self.config.agent_name,
        )
        client = await self._client_for(session)
        client.warmup()

    async def _client_for(self, session: LocalACPSession) -> DeerFlowClient:
        if self._checkpointer is None:
            raise RuntimeError("Local ACP runtime is not open")
        binding = self._client_mcp_bindings.get(session.session_id)
        key: tuple[Any, ...]
        if binding is None:
            key = ("shared", *session.runtime_key())
        else:
            key = (
                "client-mcp",
                session.session_id,
                binding.fingerprint,
                *session.runtime_key(),
            )
        async with self._client_lock:
            client = self._clients.get(key)
            if client is None:
                kwargs: dict[str, Any] = {
                    "config_path": str(self.config.config_path),
                    "checkpointer": self._checkpointer,
                    "model_name": session.model_name,
                    "thinking_enabled": session.thinking_enabled,
                    "subagent_enabled": False,
                    "plan_mode": session.plan_mode,
                    "max_concurrent_subagents": 1,
                    "recursion_limit": session.recursion_limit,
                    "agent_name": session.agent_name,
                    "checkpoint_channel_mode": "full",
                    "excluded_tool_names": _LOCAL_ACP_EXCLUDED_TOOLS,
                }
                if binding is not None:
                    from deerflow.mcp.tools import get_mcp_tools

                    kwargs["additional_mcp_tools"] = await get_mcp_tools(
                        binding.extensions_config
                    )
                client = DeerFlowClient(**kwargs)
                self._clients[key] = client
            return client

    async def bind_client_mcp(
        self,
        session_id: str,
        binding: ClientMCPBinding | None,
    ) -> None:
        """Attach an in-memory client MCP definition to one ACP session."""

        current = self._client_mcp_bindings.get(session_id)
        if current is not None and binding is not None:
            if current.fingerprint == binding.fingerprint:
                return
        if current is not None:
            await self.release_client_mcp(session_id)
        if binding is not None:
            self._client_mcp_bindings[session_id] = binding

    async def release_client_mcp(self, session_id: str) -> None:
        """Forget a session's client MCP config and close its MCP processes."""

        self._client_mcp_bindings.pop(session_id, None)
        async with self._client_lock:
            stale_keys = [
                key
                for key in self._clients
                if len(key) > 1 and key[0] == "client-mcp" and key[1] == session_id
            ]
            for key in stale_keys:
                self._clients.pop(key, None)

        from deerflow.mcp.session_pool import get_session_pool

        await get_session_pool().close_scope(session_id)

    async def astream(
        self,
        session: LocalACPSession,
        message: str,
        *,
        live_event_callback: LiveEventCallback,
    ) -> AsyncGenerator[StreamEvent, None]:
        from deerflow.config import get_app_config
        from deerflow.sandbox.provider_paths import is_host_fs_sandbox_provider_path

        from .workspace import normalize_workspace_cwd, workspace_paths_equal

        provider_path = get_app_config().sandbox.use
        if not is_host_fs_sandbox_provider_path(provider_path):
            raise RuntimeError(
                "Local ACP cwd workspaces require LocalSandboxProvider or "
                "LocalWslProvider; the configured sandbox cannot mount the "
                "client working directory"
            )
        try:
            workspace_path = normalize_workspace_cwd(session.cwd)
        except ValueError as exc:
            raise RuntimeError(f"ACP session workspace is unavailable: {exc}") from exc
        if not workspace_paths_equal(session.cwd, workspace_path):
            raise RuntimeError(
                "ACP session workspace changed after session creation: "
                f"expected {session.cwd}, resolved to {workspace_path}"
            )
        client = await self._client_for(session)
        async with self._run_slots:
            async for event in client.astream(
                message,
                thread_id=session.session_id,
                live_event_callback=live_event_callback,
                workspace_path=workspace_path,
            ):
                yield event

    async def history(self, session_id: str) -> list[dict[str, Any]]:
        """Return the latest full message snapshot for session/load replay."""

        checkpointer = self._checkpointer
        if checkpointer is None:
            raise RuntimeError("Local ACP runtime is not open")
        checkpoint = await checkpointer.aget_tuple(
            {"configurable": {"thread_id": session_id, "checkpoint_ns": ""}}
        )
        if checkpoint is None:
            return []
        messages = checkpoint.checkpoint.get("channel_values", {}).get("messages", [])
        return [
            DeerFlowClient._serialize_message(message)
            if hasattr(message, "content")
            else dict(message)
            for message in messages
            if hasattr(message, "content") or isinstance(message, dict)
        ]
