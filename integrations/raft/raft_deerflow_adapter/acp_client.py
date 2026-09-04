"""Persistent ACP v1 client for DeerFlow Portable."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import acp
from acp import Client, PROTOCOL_VERSION, text_block
from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AllowedOutcome,
    ClientCapabilities,
    Implementation,
    ResourceContentBlock,
    TextContentBlock,
    ToolCallStart,
    ToolCallUpdate,
)

from .activity import ActivityQueue


class _TurnActivity:
    def __init__(self) -> None:
        self.thinking = False
        self.response_started = False
        self.tools: dict[str, str] = {}
        self.ended_tools: set[str] = set()


class _AdapterACPClient(Client):
    def __init__(self, activity: ActivityQueue) -> None:
        self.activity = activity
        self.captures: dict[str, list[str]] = {}
        self.turns: dict[str, _TurnActivity] = {}

    def begin_turn(self, session_id: str) -> None:
        self.turns[session_id] = _TurnActivity()
        self.activity.emit("UserPromptSubmit", session_id=session_id)

    def finish_turn(self, session_id: str) -> None:
        self._end_thinking(session_id)
        self.turns.pop(session_id, None)

    def _start_thinking(self, session_id: str) -> None:
        turn = self.turns.get(session_id)
        if turn is None or turn.thinking:
            return
        turn.thinking = True
        self.activity.emit("ThinkingStart", session_id=session_id)

    def _end_thinking(self, session_id: str) -> None:
        turn = self.turns.get(session_id)
        if turn is None or not turn.thinking:
            return
        turn.thinking = False
        self.activity.emit("ThinkingEnd", session_id=session_id)

    @staticmethod
    def _tool_name(update: Any) -> str:
        return str(
            getattr(update, "title", None)
            or getattr(update, "kind", None)
            or "tool"
        )

    def _start_tool(self, session_id: str, update: Any) -> None:
        turn = self.turns.get(session_id)
        if turn is None:
            return
        self._end_thinking(session_id)
        tool_id = str(getattr(update, "tool_call_id", "unknown"))
        if tool_id in turn.tools:
            return
        tool_name = self._tool_name(update)
        turn.tools[tool_id] = tool_name
        self.activity.emit(
            "PreToolUse", session_id=session_id, tool_name=tool_name
        )

    def _update_tool(self, session_id: str, update: ToolCallUpdate) -> None:
        turn = self.turns.get(session_id)
        if turn is None:
            return
        tool_id = str(update.tool_call_id)
        if tool_id not in turn.tools:
            self._start_tool(session_id, update)
        status = getattr(update, "status", None)
        if status not in ("completed", "failed") or tool_id in turn.ended_tools:
            return
        turn.ended_tools.add(tool_id)
        tool_name = turn.tools.get(tool_id, self._tool_name(update))
        self.activity.emit(
            "PostToolUseFailure" if status == "failed" else "PostToolUse",
            session_id=session_id,
            tool_name=tool_name,
            status="error" if status == "failed" else "ok",
            error_class="tool_failure" if status == "failed" else None,
        )

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        del kwargs
        if isinstance(update, (AgentThoughtChunk, AgentPlanUpdate)):
            self._start_thinking(session_id)
            return
        if isinstance(update, ToolCallStart):
            self._start_tool(session_id, update)
            return
        if isinstance(update, ToolCallUpdate):
            self._update_tool(session_id, update)
            return
        capture = self.captures.get(session_id)
        if capture is None or not isinstance(update, AgentMessageChunk):
            return
        self._end_thinking(session_id)
        turn = self.turns.get(session_id)
        if turn is not None and not turn.response_started:
            turn.response_started = True
            self.activity.emit("ModelResponseStart", session_id=session_id)
        content = update.content
        if isinstance(content, TextContentBlock):
            capture.append(content.text)
        elif isinstance(content, ResourceContentBlock):
            capture.append(f"\n[{content.name}]({content.uri})\n")

    async def request_permission(
        self, options: list[Any], session_id: str, tool_call: Any, **kwargs: Any
    ) -> acp.RequestPermissionResponse:
        del session_id, tool_call, kwargs
        for preferred in ("allow_once", "allow_always"):
            for option in options:
                if getattr(option, "kind", None) != preferred:
                    continue
                option_id = getattr(option, "option_id", None)
                if option_id is not None:
                    return acp.RequestPermissionResponse(
                        outcome=AllowedOutcome(
                            outcome="selected", optionId=option_id
                        )
                    )
        return acp.RequestPermissionResponse(
            outcome=acp.schema.DeniedOutcome(outcome="cancelled")
        )


class DeerFlowACPClient:
    def __init__(
        self,
        command: str,
        args: list[str],
        workspace: Path,
        *,
        timeout_seconds: float = 600,
        activity: ActivityQueue | None = None,
    ) -> None:
        self.command = command
        self.args = list(args)
        self.workspace = workspace
        self.timeout_seconds = timeout_seconds
        self.activity = activity or ActivityQueue()
        self._client = _AdapterACPClient(self.activity)
        self._stack: AsyncExitStack | None = None
        self._connection: Any = None
        self._attached_sessions: set[str] = set()

    async def open(self) -> None:
        if self._stack is not None:
            return
        stack = AsyncExitStack()
        try:
            connection, _process = await stack.enter_async_context(
                acp.spawn_agent_process(
                    self._client,
                    self.command,
                    *self.args,
                    cwd=self.workspace,
                )
            )
            async with asyncio.timeout(self.timeout_seconds):
                await connection.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(
                        name="raft-deerflow-adapter",
                        title="Raft DeerFlow Adapter",
                        version="0.1.0",
                    ),
                )
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self._connection = connection

    async def close(self) -> None:
        stack, self._stack = self._stack, None
        self._connection = None
        self._attached_sessions.clear()
        if stack is not None:
            await stack.aclose()

    async def attach_or_create(self, existing_session_id: str | None) -> str:
        if self._connection is None:
            raise RuntimeError("ACP client is not open")
        if existing_session_id in self._attached_sessions:
            return existing_session_id
        if existing_session_id:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    await self._connection.load_session(
                        cwd=str(self.workspace),
                        session_id=existing_session_id,
                        mcp_servers=[],
                    )
                self._attached_sessions.add(existing_session_id)
                return existing_session_id
            except Exception:
                pass
        async with asyncio.timeout(self.timeout_seconds):
            response = await self._connection.new_session(
                cwd=str(self.workspace), mcp_servers=[]
            )
        self._attached_sessions.add(response.session_id)
        return response.session_id

    async def prompt(self, session_id: str, prompt: str) -> str:
        if self._connection is None:
            raise RuntimeError("ACP client is not open")
        chunks: list[str] = []
        self._client.captures[session_id] = chunks
        self._client.begin_turn(session_id)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self._connection.prompt(
                    session_id=session_id,
                    prompt=[text_block(prompt)],
                )
            await asyncio.sleep(0)
            return "".join(chunks).strip()
        finally:
            self._client.finish_turn(session_id)
            self._client.captures.pop(session_id, None)
