"""ACP v1 Agent implementation backed by :class:`DeerFlowClient`."""

from __future__ import annotations

import asyncio
import importlib.metadata
import uuid
from typing import Any

import acp
from acp import RequestError, schema

from .client_mcp import ClientMCPBinding, normalize_client_mcp_servers
from .config import LocalACPConfig
from .event_mapper import ACPEventMapper, _display_text, _message_uuid
from .runtime import LocalACPRuntime
from .session_store import LocalACPSession, LocalACPSessionStore
from .workspace import normalize_workspace_cwd, workspace_paths_equal


class DeerFlowACPAgent:
    """Local, text-only ACP Agent bound to the client's project workspace."""

    def __init__(
        self,
        config: LocalACPConfig,
        store: LocalACPSessionStore,
        runtime: LocalACPRuntime,
    ):
        self.config = config
        self.store = store
        self.runtime = runtime
        self._connection: Any = None
        self._active: dict[str, asyncio.Task[Any]] = {}
        self._active_lock = asyncio.Lock()
        self._client_mcp_sessions: set[str] = set()

    def on_connect(self, conn: Any) -> None:
        self._connection = conn

    @staticmethod
    def _implementation_version() -> str:
        try:
            return importlib.metadata.version("deerflow-api")
        except importlib.metadata.PackageNotFoundError:
            return "0.1.0"

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: schema.ClientCapabilities | None = None,
        client_info: schema.Implementation | None = None,
        **kwargs: Any,
    ) -> acp.InitializeResponse:
        del protocol_version, client_capabilities, client_info, kwargs
        return acp.InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_capabilities=schema.AgentCapabilities(
                load_session=True,
                mcp_capabilities=schema.McpCapabilities(http=False, sse=False),
                prompt_capabilities=schema.PromptCapabilities(
                    audio=False,
                    embedded_context=False,
                    image=False,
                ),
                session_capabilities=schema.SessionCapabilities(
                    list=schema.SessionListCapabilities(),
                ),
            ),
            agent_info=schema.Implementation(
                name="deerflow-local",
                title="DeerFlow Local Tasks",
                version=self._implementation_version(),
            ),
            auth_methods=[],
        )

    def _reject_client_resources(
        self,
        additional_directories: list[str] | None,
        mcp_servers: list[Any] | None,
    ) -> ClientMCPBinding | None:
        if additional_directories:
            raise RequestError.invalid_params(
                {"details": "This local DeerFlow agent does not access client additionalDirectories"}
            )
        try:
            return normalize_client_mcp_servers(
                mcp_servers,
                enabled=self.config.accept_client_mcp_servers,
            )
        except ValueError as exc:
            raise RequestError.invalid_params(
                {"details": str(exc)}
            ) from exc

    def _defaults(self) -> dict[str, Any]:
        return {
            "model_name": self.config.model_name,
            "thinking_enabled": self.config.thinking_enabled,
            "subagent_enabled": False,
            "plan_mode": self.config.plan_mode,
            "max_concurrent_subagents": 1,
            "recursion_limit": self.config.recursion_limit,
            "agent_name": self.config.agent_name,
        }

    @staticmethod
    def _mode_state(session: LocalACPSession) -> schema.SessionModeState:
        return schema.SessionModeState(
            current_mode_id="plan" if session.plan_mode else "default",
            available_modes=[
                schema.SessionMode(
                    id="default",
                    name="Default",
                    description="Execute a general task directly.",
                ),
                schema.SessionMode(
                    id="plan",
                    name="Plan",
                    description="Track multi-step work as an ACP plan.",
                ),
            ],
        )

    @staticmethod
    def _config_options(
        session: LocalACPSession,
    ) -> list[
        schema.SessionConfigOptionSelect | schema.SessionConfigOptionBoolean
    ]:
        enabled_options = [
            schema.SessionConfigSelectOption(
                name="On",
                value="on",
            ),
            schema.SessionConfigSelectOption(
                name="Off",
                value="off",
            ),
        ]
        return [
            schema.SessionConfigOptionSelect(
                type="select",
                id="thinking_enabled",
                name="Thinking",
                description="Show model reasoning updates when available.",
                current_value="on" if session.thinking_enabled else "off",
                options=enabled_options,
            ),
        ]

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> acp.NewSessionResponse:
        del kwargs
        client_mcp = self._reject_client_resources(additional_directories, mcp_servers)
        try:
            workspace_cwd = normalize_workspace_cwd(cwd)
        except ValueError as exc:
            raise RequestError.invalid_params({"details": str(exc)}) from exc
        session = await self.store.create(cwd=workspace_cwd, defaults=self._defaults())
        await self.runtime.bind_client_mcp(session.session_id, client_mcp)
        if client_mcp is not None:
            self._client_mcp_sessions.add(session.session_id)
        return acp.NewSessionResponse(
            session_id=session.session_id,
            modes=self._mode_state(session),
            config_options=self._config_options(session),
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> acp.LoadSessionResponse:
        del kwargs
        client_mcp = self._reject_client_resources(additional_directories, mcp_servers)
        session = await self._require_session(session_id)
        try:
            stored_workspace_cwd = normalize_workspace_cwd(session.cwd)
        except ValueError as exc:
            raise RequestError.invalid_params(
                {"details": f"Stored ACP session workspace is unavailable: {exc}"}
            ) from exc
        try:
            workspace_cwd = normalize_workspace_cwd(cwd)
        except ValueError as exc:
            raise RequestError.invalid_params({"details": str(exc)}) from exc
        if not workspace_paths_equal(stored_workspace_cwd, workspace_cwd):
            raise RequestError.invalid_params(
                {
                    "details": (
                        "ACP session workspace does not match cwd: "
                        f"expected {stored_workspace_cwd}, received {workspace_cwd}"
                    )
                }
            )
        if session.cwd != stored_workspace_cwd:
            session.cwd = stored_workspace_cwd
            await self.store.save(session)
        await self.runtime.bind_client_mcp(session_id, client_mcp)
        if client_mcp is None:
            self._client_mcp_sessions.discard(session_id)
        else:
            self._client_mcp_sessions.add(session_id)
        for index, message in enumerate(await self.runtime.history(session_id)):
            message_type = message.get("type")
            raw_id = message.get("id") or f"history-{index}"
            text = _display_text(message.get("content"))
            if not text:
                continue
            if message_type == "human":
                update: Any = schema.UserMessageChunk(
                    session_update="user_message_chunk",
                    content=acp.text_block(text),
                    message_id=_message_uuid("history-user", raw_id),
                )
            elif message_type == "ai":
                reasoning = message.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    await self._session_update(
                        session_id,
                        schema.AgentThoughtChunk(
                            session_update="agent_thought_chunk",
                            content=acp.text_block(reasoning),
                            message_id=_message_uuid("history-thought", raw_id),
                        ),
                    )
                update = schema.AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=acp.text_block(text),
                    message_id=_message_uuid("history-agent", raw_id),
                )
            else:
                continue
            await self._session_update(session_id, update)
        return acp.LoadSessionResponse(
            modes=self._mode_state(session),
            config_options=self._config_options(session),
        )

    async def list_sessions(
        self,
        additional_directories: list[str] | None = None,
        cursor: str | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> schema.ListSessionsResponse:
        del kwargs
        self._reject_client_resources(additional_directories, None)
        try:
            normalized_cwd = normalize_workspace_cwd(cwd) if cwd is not None else None
            sessions, next_cursor = await self.store.list(
                cwd=normalized_cwd,
                cursor=cursor,
                limit=self.config.session_page_size,
            )
        except ValueError as exc:
            raise RequestError.invalid_params({"details": str(exc)}) from exc
        return schema.ListSessionsResponse(
            sessions=[
                schema.SessionInfo(
                    session_id=session.session_id,
                    cwd=session.cwd,
                    title=session.title,
                    updated_at=session.updated_at,
                )
                for session in sessions
            ],
            next_cursor=next_cursor,
        )

    async def set_session_mode(
        self,
        mode_id: str,
        session_id: str,
        **kwargs: Any,
    ) -> acp.SetSessionModeResponse:
        del kwargs
        if mode_id not in {"default", "plan"}:
            raise RequestError.invalid_params({"details": f"Unknown mode: {mode_id}"})
        session = await self._require_idle_session(session_id)
        session.plan_mode = mode_id == "plan"
        await self.store.save(session)
        return acp.SetSessionModeResponse()

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **kwargs: Any,
    ) -> acp.SetSessionConfigOptionResponse:
        del kwargs
        if config_id != "thinking_enabled":
            raise RequestError.invalid_params(
                {"details": f"Unsupported config option: {config_id}"}
            )
        if isinstance(value, bool):
            enabled = value
        elif value in {"on", "off"}:
            enabled = value == "on"
        else:
            raise RequestError.invalid_params(
                {"details": f"Unsupported value for config option {config_id}: {value}"}
            )
        session = await self._require_idle_session(session_id)
        setattr(session, config_id, enabled)
        await self.store.save(session)
        return acp.SetSessionConfigOptionResponse(
            config_options=self._config_options(session)
        )

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> acp.PromptResponse:
        del kwargs
        session = await self._require_session(session_id)
        text_parts: list[str] = []
        for block in prompt:
            if not isinstance(block, schema.TextContentBlock):
                raise RequestError.invalid_params(
                    {"details": "Only ACP text prompt blocks are supported"}
                )
            if block.text:
                text_parts.append(block.text)
        message = "\n".join(text_parts).strip()
        if not message:
            raise RequestError.invalid_params({"details": "Prompt text must not be empty"})

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("ACP prompt is not running in an asyncio task")
        async with self._active_lock:
            if session_id in self._active:
                raise RequestError(-32001, "Session already has an active prompt", {"sessionId": session_id})
            self._active[session_id] = task

        mapper = ACPEventMapper(
            session_id,
            lambda update: self._session_update(session_id, update),
        )
        cancelled = False
        try:
            async with asyncio.timeout(self.config.run_timeout_seconds):
                async for event in self.runtime.astream(
                    session,
                    message,
                    live_event_callback=mapper.handle_live,
                ):
                    await mapper.handle(event)
            if mapper.failure_message:
                raise RequestError.internal_error({"details": mapper.failure_message})
            await mapper.close_open_tools(cancelled=False)
        except asyncio.CancelledError:
            cancelled = True
            if hasattr(task, "uncancel"):
                while task.cancelling():
                    task.uncancel()
            await mapper.close_open_tools(cancelled=True)
        except TimeoutError as exc:
            await mapper.close_open_tools(cancelled=True)
            raise RequestError.internal_error(
                {"details": f"DeerFlow task timed out after {self.config.run_timeout_seconds:g} seconds"}
            ) from exc
        except RequestError:
            await mapper.close_open_tools(cancelled=True)
            raise
        except Exception:
            await mapper.close_open_tools(cancelled=True)
            raise
        finally:
            async with self._active_lock:
                if self._active.get(session_id) is task:
                    self._active.pop(session_id, None)

        if mapper.title:
            session.title = mapper.title
        await self.store.save(session)
        usage = schema.Usage(
            input_tokens=mapper.usage["input_tokens"],
            output_tokens=mapper.usage["output_tokens"],
            total_tokens=mapper.usage["total_tokens"],
        )
        return acp.PromptResponse(
            stop_reason="cancelled" if cancelled else "end_turn",
            usage=usage,
            user_message_id=message_id or str(uuid.uuid4()),
        )

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        del kwargs
        async with self._active_lock:
            task = self._active.get(session_id)
            if task is not None and not task.done():
                task.cancel()

    async def shutdown(self) -> None:
        async with self._active_lock:
            tasks = [task for task in self._active.values() if not task.done()]
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        sessions = list(self._client_mcp_sessions)
        self._client_mcp_sessions.clear()
        if sessions:
            await asyncio.gather(
                *(self.runtime.release_client_mcp(session_id) for session_id in sessions),
                return_exceptions=True,
            )

    async def _session_update(self, session_id: str, update: Any) -> None:
        if self._connection is None:
            raise RuntimeError("ACP client connection is not available")
        await self._connection.session_update(session_id=session_id, update=update)

    async def _require_session(self, session_id: str) -> LocalACPSession:
        session = await self.store.get(session_id)
        if session is None:
            raise RequestError.resource_not_found(f"session:{session_id}")
        return session

    async def _require_idle_session(self, session_id: str) -> LocalACPSession:
        session = await self._require_session(session_id)
        async with self._active_lock:
            if session_id in self._active:
                raise RequestError(-32001, "Session has an active prompt", {"sessionId": session_id})
        return session

    async def set_session_model(
        self, model_id: str, session_id: str, **kwargs: Any
    ) -> schema.SetSessionModelResponse:
        del model_id, session_id, kwargs
        raise RequestError.method_not_found("session/set_model")

    async def authenticate(self, method_id: str, **kwargs: Any) -> acp.AuthenticateResponse | None:
        del method_id, kwargs
        raise RequestError.method_not_found("authenticate")

    async def fork_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.ForkSessionResponse:
        del cwd, session_id, additional_directories, mcp_servers, kwargs
        raise RequestError.method_not_found("session/fork")

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.ResumeSessionResponse:
        del cwd, session_id, additional_directories, mcp_servers, kwargs
        raise RequestError.method_not_found("session/resume")

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> schema.CloseSessionResponse | None:
        del session_id, kwargs
        raise RequestError.method_not_found("session/close")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del params
        raise RequestError.method_not_found(f"_{method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        del method, params
