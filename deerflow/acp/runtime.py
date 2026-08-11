"""Embedded DeerFlow runtime used by the local ACP adapter."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from deerflow.client import DeerFlowClient, StreamEvent

from .client_mcp import ClientMCPBinding
from .config import LocalACPConfig
from .permission import ACPPermissionBroker, ACPPermissionMiddleware, PermissionHandler
from .policy import LocalACPCapabilityPolicy
from .session_coordinator import ACPSessionCoordinator
from .session_store import LocalACPSession

LiveEventCallback = Callable[[dict[str, Any]], Awaitable[None]]

logger = logging.getLogger(__name__)


class LocalACPRuntime:
    """Owns the ACP-only checkpointer and embedded DeerFlow clients."""

    def __init__(self, config: LocalACPConfig):
        self.config = config
        self.policy = LocalACPCapabilityPolicy.from_config(config)
        self.session_coordinator = ACPSessionCoordinator()
        self.permission_broker = ACPPermissionBroker(
            self.policy,
            session_owner=self.session_coordinator.owner,
        )
        self.permission_middleware = ACPPermissionMiddleware(self.permission_broker)
        self._checkpointer_cm: AbstractAsyncContextManager[Any] | None = None
        self._checkpointer: Any = None
        self._clients: dict[tuple[Any, ...], DeerFlowClient] = {}
        self._client_mcp_bindings: dict[str, ClientMCPBinding] = {}
        self._client_lock = asyncio.Lock()
        self._run_slots = asyncio.Semaphore(config.max_active_runs)

    def bind_permission_handler(
        self, connection_id: str, handler: PermissionHandler
    ) -> None:
        self.permission_broker.bind(handler, connection_id)

    def unbind_permission_handler(self, connection_id: str) -> None:
        self.permission_broker.unbind(connection_id)

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
        if client_mcp_sessions:
            results = await asyncio.gather(
                *(
                    self.release_client_mcp(session_id)
                    for session_id in client_mcp_sessions
                ),
                return_exceptions=True,
            )
            for session_id, result in zip(client_mcp_sessions, results, strict=True):
                if isinstance(result, BaseException):
                    logger.warning(
                        "Failed to release client MCP while closing ACP runtime for session %s",
                        session_id,
                        exc_info=(type(result), result, result.__traceback__),
                    )
        self._clients.clear()
        cm = self._checkpointer_cm
        self._checkpointer_cm = None
        self._checkpointer = None
        try:
            if cm is not None:
                await cm.__aexit__(None, None, None)
        finally:
            await self._flush_memory()

    async def _flush_memory(self) -> None:
        try:
            from deerflow.agents.memory import get_memory_manager, reset_memory_manager
            from deerflow.config.memory_config import get_memory_config

            memory_config = get_memory_config()
            if not memory_config.enabled:
                return
            manager = get_memory_manager()
            flushed = await asyncio.wait_for(
                asyncio.to_thread(manager.shutdown_flush),
                timeout=memory_config.shutdown_flush_timeout_seconds + 1.0,
            )
            if flushed is not False:
                await asyncio.to_thread(reset_memory_manager)
            else:
                logger.warning("ACP memory queue did not drain during shutdown")
        except TimeoutError:
            logger.warning("ACP memory shutdown flush timed out")
        except Exception:
            logger.warning("ACP memory shutdown flush failed", exc_info=True)

    async def warmup(self) -> None:
        """Build and cache the default DeerFlow client graph without calling a model."""

        session = LocalACPSession(
            session_id="__deerflow_acp_warmup__",
            cwd="",
            title=None,
            updated_at="",
            model_name=self.config.model_name,
            thinking_enabled=self.config.thinking_enabled,
            subagent_enabled=self.config.subagent_enabled,
            plan_mode=self.config.plan_mode,
            max_concurrent_subagents=self.config.max_concurrent_subagents,
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
                effective_subagents = (
                    session.subagent_enabled and self.policy.subagents_enabled
                )
                kwargs: dict[str, Any] = {
                    "config_path": str(self.config.config_path),
                    "checkpointer": self._checkpointer,
                    "model_name": session.model_name,
                    "thinking_enabled": session.thinking_enabled,
                    "subagent_enabled": effective_subagents,
                    "plan_mode": session.plan_mode,
                    "max_concurrent_subagents": (
                        session.max_concurrent_subagents if effective_subagents else 1
                    ),
                    "recursion_limit": session.recursion_limit,
                    "agent_name": session.agent_name,
                    "checkpoint_channel_mode": "full",
                    "excluded_tool_names": self.policy.excluded_tool_names(
                        enable_bash=self.config.enable_bash
                    ),
                    "allowed_tool_names": (
                        set(self.policy.tool_allowlist)
                        if self.policy.tool_allowlist is not None
                        else None
                    ),
                    "system_prompt_overlay": self.policy.prompt_overlay(),
                    "subagent_system_prompt_overlay": self.policy.prompt_overlay(
                        for_subagent=True
                    ),
                    "middlewares": [self.permission_middleware],
                    "subagent_middlewares": [self.permission_middleware],
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
        if (
            current is not None
            and binding is not None
            and current.fingerprint == binding.fingerprint
        ):
            return
        if current is not None:
            await self.release_client_mcp(session_id)
        if binding is not None:
            self._client_mcp_bindings[session_id] = binding

    async def release_client_mcp(self, session_id: str) -> None:
        """Forget a session's client MCP config and close its MCP processes."""

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
        # Keep the binding as a retry marker if close_scope raises. A later
        # session load or daemon shutdown will attempt the cleanup again.
        self._client_mcp_bindings.pop(session_id, None)

    async def release_session(self, session_id: str) -> None:
        try:
            await self.release_client_mcp(session_id)
        finally:
            self.permission_broker.clear_session(session_id)

    async def purge_checkpoints(self, session_ids: list[str]) -> None:
        checkpointer = self._checkpointer
        if checkpointer is None:
            return
        for session_id in session_ids:
            try:
                await checkpointer.adelete_thread(session_id)
            except Exception:
                logger.warning(
                    "Failed to purge ACP checkpoints for session %s",
                    session_id,
                    exc_info=True,
                )

    def _memory_user_id(
        self, session: LocalACPSession, workspace_path: str
    ) -> str | None:
        if self.config.memory_scope == "global":
            return None
        if self.config.memory_scope == "session":
            return f"acp-session:{session.session_id}"
        digest = hashlib.sha256(workspace_path.encode("utf-8")).hexdigest()[:24]
        return f"acp-workspace:{digest}"

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
                user_id=self._memory_user_id(session, workspace_path),
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

    async def history_state(self, session_id: str) -> dict[str, Any]:
        """Return replayable messages plus plan, artifact, and title state."""

        checkpointer = self._checkpointer
        if checkpointer is None:
            raise RuntimeError("Local ACP runtime is not open")
        checkpoint = await checkpointer.aget_tuple(
            {"configurable": {"thread_id": session_id, "checkpoint_ns": ""}}
        )
        if checkpoint is None:
            return {"messages": [], "todos": [], "artifacts": []}
        values = checkpoint.checkpoint.get("channel_values", {})
        messages = values.get("messages", [])
        return {
            "messages": [
                DeerFlowClient._serialize_message(message)
                if hasattr(message, "content")
                else dict(message)
                for message in messages
                if hasattr(message, "content") or isinstance(message, dict)
            ],
            "todos": values.get("todos", []),
            "artifacts": values.get("artifacts", []),
            "title": values.get("title"),
        }
