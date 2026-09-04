"""Stamp tool results and expose their receipt ledger to subagents."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.agents.middlewares.tool_receipt import (
    TOOL_RECEIPT_KEY,
    TOOL_RECEIPT_LEDGER_KEY,
    ToolReceipt,
    extract_tool_receipts,
    make_tool_receipt,
    render_tool_receipts_with_snapshot,
)

logger = logging.getLogger(__name__)


class ToolReceiptMiddleware(AgentMiddleware[AgentState]):
    """Create execution receipts and render a bounded ledger on model calls."""

    state_schema = AgentState

    @staticmethod
    def _stamp_message(message: ToolMessage, request: ToolCallRequest) -> None:
        try:
            kwargs = dict(message.additional_kwargs or {})
            kwargs[TOOL_RECEIPT_KEY] = make_tool_receipt(request.tool_call, message)
            message.additional_kwargs = kwargs
        except Exception:
            logger.warning("Failed to stamp tool receipt", exc_info=True)

    def _stamp(
        self,
        result: ToolMessage | Command,
        request: ToolCallRequest,
    ) -> ToolMessage | Command:
        if isinstance(result, ToolMessage):
            self._stamp_message(result, request)
            return result
        update = result.update
        if not isinstance(update, dict):
            return result
        messages = update.get("messages", [])
        if isinstance(messages, ToolMessage):
            messages = [messages]
        if not isinstance(messages, (list, tuple)):
            return result
        tool_call_id = str(request.tool_call.get("id") or "")
        for message in messages:
            if (
                isinstance(message, ToolMessage)
                and str(message.tool_call_id) == tool_call_id
            ):
                self._stamp_message(message, request)
        return result

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        return self._stamp(handler(request), request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        return self._stamp(await handler(request), request)

    @staticmethod
    def _inject(request: ModelRequest, ledger: str) -> ModelRequest:
        if not ledger:
            return request
        message = HumanMessage(
            content=ledger,
            additional_kwargs={
                "hide_from_ui": True,
                "deerflow_tool_receipt_context": True,
            },
        )
        messages = list(request.messages)
        insert_at = 0
        while insert_at < len(messages) and messages[insert_at].type == "system":
            insert_at += 1
        messages.insert(insert_at, message)
        return request.override(messages=messages)

    def _prepare_model_call(
        self,
        request: ModelRequest,
    ) -> tuple[ModelRequest, list[ToolReceipt]]:
        ledger, visible = render_tool_receipts_with_snapshot(
            extract_tool_receipts(list(request.messages))
        )
        return self._inject(request, ledger), visible

    @staticmethod
    def _stamp_visible_ledger(
        result: ModelCallResult,
        receipts: list[ToolReceipt],
    ) -> ModelCallResult:
        if isinstance(result, AIMessage):
            messages = [result]
        else:
            response = getattr(result, "model_response", result)
            messages = getattr(response, "result", [])
        for message in messages:
            if isinstance(message, AIMessage):
                kwargs = dict(message.additional_kwargs or {})
                kwargs[TOOL_RECEIPT_LEDGER_KEY] = [dict(item) for item in receipts]
                message.additional_kwargs = kwargs
        return result

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        prepared, receipts = self._prepare_model_call(request)
        return self._stamp_visible_ledger(handler(prepared), receipts)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        prepared, receipts = self._prepare_model_call(request)
        return self._stamp_visible_ledger(await handler(prepared), receipts)
