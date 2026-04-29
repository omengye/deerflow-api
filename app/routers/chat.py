"""Chat endpoints — sync and streaming."""
import asyncio
import json
import uuid
from typing import Any

import logging

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse, Response
from sse_starlette.sse import EventSourceResponse

from app.config import settings
from app.dependencies import get_client_manager

logger = logging.getLogger(__name__)
from app.schemas import AguiRunAgentInput, ChatRequest, ChatResponse
from deerflow.client import StreamEvent

router = APIRouter(tags=["chat"])

_CHAT_STREAM_OPTIONS_HEADERS = {
    "Allow": "OPTIONS, POST",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "OPTIONS, POST",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Max-Age": "86400",
}

_AGUI_OPTIONS_HEADERS = {
    "Allow": "OPTIONS, POST",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "OPTIONS, POST",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, Accept",
    "Access-Control-Max-Age": "86400",
}


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest = Body()):
    """Sync chat — returns final response text."""
    manager = get_client_manager()
    client = manager.get_client()

    thread_id = req.thread_id or str(uuid.uuid4())
    manager.mark_thread_running(thread_id)

    kwargs = {}
    if req.model_name:
        kwargs["model_name"] = req.model_name
    if req.thinking_enabled is not None:
        kwargs["thinking_enabled"] = req.thinking_enabled
    if req.subagent_enabled is not None:
        kwargs["subagent_enabled"] = req.subagent_enabled
    if req.plan_mode is not None:
        kwargs["plan_mode"] = req.plan_mode
    if req.max_concurrent_subagents is not None:
        kwargs["max_concurrent_subagents"] = req.max_concurrent_subagents
    if req.agent_name:
        kwargs["agent_name"] = req.agent_name

    try:
        content = await asyncio.wait_for(
            asyncio.to_thread(client.chat, req.message, thread_id=thread_id, **kwargs),
            timeout=settings.chat_request_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "chat request timed out after %.0fs (thread=%s)",
            settings.chat_request_timeout, thread_id,
        )
        raise HTTPException(status_code=504, detail="Chat request timed out")
    except Exception:
        logger.exception("Unhandled error in /chat (thread=%s)", thread_id)
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        manager.mark_thread_done(thread_id)

    title = await asyncio.to_thread(_get_thread_title, client, thread_id)

    return ChatResponse(
        thread_id=thread_id,
        content=content,
        title=title,
    )


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest = Body()):
    """Streaming chat — Server-Sent Events with token-level deltas."""
    manager = get_client_manager()
    client = await manager.get_async_client()

    thread_id = req.thread_id or str(uuid.uuid4())
    manager.mark_thread_running(thread_id)

    kwargs = {}
    if req.model_name:
        kwargs["model_name"] = req.model_name
    if req.thinking_enabled is not None:
        kwargs["thinking_enabled"] = req.thinking_enabled
    if req.subagent_enabled is not None:
        kwargs["subagent_enabled"] = req.subagent_enabled
    if req.plan_mode is not None:
        kwargs["plan_mode"] = req.plan_mode
    if req.max_concurrent_subagents is not None:
        kwargs["max_concurrent_subagents"] = req.max_concurrent_subagents
    if req.agent_name:
        kwargs["agent_name"] = req.agent_name

    async def event_generator():
        try:
            async for event in client.astream(req.message, thread_id=thread_id, **kwargs):
                yield {
                    "event": event.type,
                    "data": json.dumps(event.data, ensure_ascii=False, default=str),
                }

                # Simplified text event for easier client consumption
                if event.type == "messages-tuple" and event.data:
                    msg = event.data
                    if isinstance(msg, dict) and msg.get("type") == "ai" and msg.get("content") and not msg.get("tool_calls"):
                        yield {
                            "event": "text",
                            "data": json.dumps(
                                {
                                    "content": msg.get("content", ""),
                                    "thread_id": thread_id,
                                },
                                ensure_ascii=False,
                            ),
                        }
        except Exception:
            logger.exception("Unhandled error in /chat/stream (thread=%s)", thread_id)
            yield {
                "event": "error",
                "data": json.dumps({"error": "Internal server error"}),
            }
        finally:
            manager.mark_thread_done(thread_id)

    return EventSourceResponse(event_generator())


@router.options("/chat/stream")
async def chat_stream_options():
    """CORS preflight support for the streaming chat endpoint."""
    return Response(status_code=204, headers=_CHAT_STREAM_OPTIONS_HEADERS)


def _get_thread_title(client: Any, thread_id: str) -> str | None:
    """Try to get the thread title from thread state."""
    try:
        thread = client.get_thread(thread_id)
        if thread.get("checkpoints"):
            return thread["checkpoints"][-1].get("values", {}).get("title")
        if "values" in thread:
            return thread["values"].get("title")
    except Exception:
        pass
    return None


@router.post("/chat/agui")
async def chat_agui(req: AguiRunAgentInput = Body()):
    """AG-UI compatible chat stream.

    The wire contract follows AG-UI HTTP SSE: each chunk is a `data:` JSON
    object whose payload contains a standard AG-UI `type` field.
    """
    manager = get_client_manager()
    client = await manager.get_async_client()
    thread_id = req.thread_id
    run_id = req.run_id
    manager.mark_thread_running(thread_id)

    kwargs = _chat_kwargs_from_agui(req)
    user_message = _latest_user_message(req)
    if user_message is None:
        manager.mark_thread_done(thread_id)
        raise HTTPException(status_code=400, detail="No user message found in request")

    async def event_generator():
        open_text_message_id: str | None = None
        # Maps tool_call_id → serialized args JSON already sent as delta.
        # Used to compute incremental deltas across streaming chunks.
        tool_call_args_state: dict[str, str] = {}
        # Tool calls that have been started (TOOL_CALL_START emitted) but not yet ended.
        open_tool_call_ids: set[str] = set()
        # Signature of the last emitted MESSAGES_SNAPSHOT (list of message ids).
        # LangGraph fires a values event after every middleware node even when
        # messages haven't changed, so we deduplicate here.
        last_snapshot_sig: str | None = None
        try:
            yield _agui_sse({"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id, **({"parentRunId": req.parent_run_id} if req.parent_run_id else {})})
            async for event in client.astream(user_message, thread_id=thread_id, **kwargs):
                if event.type == "end":
                    if open_text_message_id:
                        yield _agui_sse({"type": "TEXT_MESSAGE_END", "messageId": open_text_message_id})
                        open_text_message_id = None
                    for tc_id in list(open_tool_call_ids):
                        yield _agui_sse({"type": "TOOL_CALL_ARGS", "toolCallId": tc_id, "delta": tool_call_args_state.get(tc_id, "{}")})
                        yield _agui_sse({"type": "TOOL_CALL_END", "toolCallId": tc_id})
                    open_tool_call_ids.clear()
                for agui_event in _stream_event_to_agui(event, thread_id, run_id, open_text_message_id, tool_call_args_state, open_tool_call_ids):
                    event_type = agui_event.get("type")
                    if event_type == "MESSAGES_SNAPSHOT":
                        sig = ",".join(m.get("id", "") for m in agui_event.get("messages", []))
                        if sig == last_snapshot_sig:
                            continue
                        last_snapshot_sig = sig
                    elif event_type == "TEXT_MESSAGE_START":
                        open_text_message_id = str(agui_event["messageId"])
                    elif event_type == "TEXT_MESSAGE_END":
                        open_text_message_id = None
                    yield _agui_sse(agui_event)
            for tc_id in list(open_tool_call_ids):
                yield _agui_sse({"type": "TOOL_CALL_ARGS", "toolCallId": tc_id, "delta": tool_call_args_state.get(tc_id, "{}")})
                yield _agui_sse({"type": "TOOL_CALL_END", "toolCallId": tc_id})
            open_tool_call_ids.clear()
            if open_text_message_id:
                yield _agui_sse({"type": "TEXT_MESSAGE_END", "messageId": open_text_message_id})
            yield _agui_sse({"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id})
        except Exception:
            logger.exception("Unhandled error in /chat/agui (thread=%s)", thread_id)
            for tc_id in list(open_tool_call_ids):
                yield _agui_sse({"type": "TOOL_CALL_ARGS", "toolCallId": tc_id, "delta": tool_call_args_state.get(tc_id, "{}")})
                yield _agui_sse({"type": "TOOL_CALL_END", "toolCallId": tc_id})
            if open_text_message_id:
                yield _agui_sse({"type": "TEXT_MESSAGE_END", "messageId": open_text_message_id})
            yield _agui_sse({"type": "RUN_ERROR", "message": "Internal server error"})
        finally:
            manager.mark_thread_done(thread_id)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.options("/chat/agui")
async def chat_agui_options():
    """CORS preflight support for the AG-UI chat endpoint."""
    return Response(status_code=204, headers=_AGUI_OPTIONS_HEADERS)


def _chat_kwargs_from_agui(req: AguiRunAgentInput) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if req.model_name:
        kwargs["model_name"] = req.model_name
    if req.thinking_enabled is not None:
        kwargs["thinking_enabled"] = req.thinking_enabled
    if req.subagent_enabled is not None:
        kwargs["subagent_enabled"] = req.subagent_enabled
    if req.plan_mode is not None:
        kwargs["plan_mode"] = req.plan_mode
    if req.max_concurrent_subagents is not None:
        kwargs["max_concurrent_subagents"] = req.max_concurrent_subagents
    if req.agent_name:
        kwargs["agent_name"] = req.agent_name
    return kwargs


def _latest_user_message(req: AguiRunAgentInput) -> str | None:
    for message in reversed(req.messages):
        if message.role == "user" and message.content:
            return message.content
    return None


def _agui_sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def _stream_event_to_agui(
    event: StreamEvent,
    thread_id: str,
    run_id: str,
    open_text_message_id: str | None,
    tool_call_args_state: dict[str, str],
    open_tool_call_ids: set[str],
) -> list[dict[str, Any]]:
    if event.type == "messages-tuple":
        return _message_tuple_to_agui(event.data, open_text_message_id, tool_call_args_state, open_tool_call_ids)
    if event.type == "values":
        messages = event.data.get("messages")
        if isinstance(messages, list):
            return [{"type": "MESSAGES_SNAPSHOT", "messages": [_message_to_agui(m) for m in messages if isinstance(m, dict)]}]
    if event.type == "custom":
        return [{"type": "CUSTOM", "name": "deerflow.custom", "value": event.data}]
    if event.type == "end":
        return [{"type": "CUSTOM", "name": "deerflow.usage", "value": event.data.get("usage", {})}]
    return [{"type": "RAW", "source": "deerflow", "event": {"threadId": thread_id, "runId": run_id, "type": event.type, "data": event.data}}]


def _message_tuple_to_agui(
    data: dict[str, Any],
    open_text_message_id: str | None,
    tool_call_args_state: dict[str, str],
    open_tool_call_ids: set[str],
) -> list[dict[str, Any]]:
    message_type = data.get("type")
    if message_type == "ai":
        tool_calls = data.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            events: list[dict[str, Any]] = []
            if open_text_message_id:
                events.append({"type": "TEXT_MESSAGE_END", "messageId": open_text_message_id})
            events.extend(_tool_calls_to_agui(tool_calls, str(data.get("id") or uuid.uuid4()), tool_call_args_state, open_tool_call_ids))
            return events

        content = data.get("content")
        if isinstance(content, str) and content:
            message_id = str(data.get("id") or uuid.uuid4())
            events: list[dict[str, Any]] = []
            if open_text_message_id != message_id:
                if open_text_message_id:
                    events.append({"type": "TEXT_MESSAGE_END", "messageId": open_text_message_id})
                events.append({"type": "TEXT_MESSAGE_START", "messageId": message_id, "role": "assistant"})
            events.append({"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": content})
            return events

    if message_type == "tool":
        tool_call_id = data.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            events = []
            if tool_call_id in open_tool_call_ids:
                events.append({"type": "TOOL_CALL_ARGS", "toolCallId": tool_call_id, "delta": tool_call_args_state.get(tool_call_id, "{}")})
                events.append({"type": "TOOL_CALL_END", "toolCallId": tool_call_id})
                open_tool_call_ids.discard(tool_call_id)
            events.append({
                "type": "TOOL_CALL_RESULT",
                "messageId": str(data.get("id") or uuid.uuid4()),
                "toolCallId": tool_call_id,
                "content": str(data.get("content") or ""),
                "role": "tool",
            })
            return events
    return []


def _tool_calls_to_agui(
    tool_calls: list[Any],
    parent_message_id: str,
    tool_call_args_state: dict[str, str],
    open_tool_call_ids: set[str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        tool_call_id = str(tool_call.get("id") or uuid.uuid4())
        args = tool_call.get("args")
        full_json = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False, default=str)

        if tool_call_id not in tool_call_args_state:
            # First chunk — emit START and record state. TOOL_CALL_ARGS is intentionally
            # deferred: intermediate chunks serialize partial dicts to closed JSON objects
            # (e.g. "{}") that are not valid prefixes of the final JSON, so incremental
            # deltas would produce invalid output. The complete args are sent once, just
            # before TOOL_CALL_END, when the tool result or stream-end is reached.
            tool_call_name = str(tool_call.get("name") or "tool")
            tool_call_args_state[tool_call_id] = full_json
            open_tool_call_ids.add(tool_call_id)
            events.append({"type": "TOOL_CALL_START", "toolCallId": tool_call_id, "toolCallName": tool_call_name, "parentMessageId": parent_message_id})
        else:
            # Subsequent chunk — keep the latest (most complete) serialized args.
            tool_call_args_state[tool_call_id] = full_json
    return events


def _message_to_agui(message: dict[str, Any]) -> dict[str, Any]:
    role = str(message.get("type") or "assistant")
    if role == "ai":
        role = "assistant"
    result: dict[str, Any] = {
        "id": str(message.get("id") or uuid.uuid4()),
        "role": role,
        "content": str(message.get("content") or ""),
    }
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        result["toolCalls"] = [_tool_call_to_agui_message(tc) for tc in tool_calls if isinstance(tc, dict)]
    if tool_call_id := message.get("tool_call_id"):
        result["toolCallId"] = str(tool_call_id)
    if name := message.get("name"):
        result["name"] = str(name)
    return result


def _tool_call_to_agui_message(tool_call: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(tool_call.get("id") or uuid.uuid4()),
        "type": "function",
        "function": {
            "name": str(tool_call.get("name") or "tool"),
            "arguments": json.dumps(tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}, ensure_ascii=False, default=str),
        },
    }
