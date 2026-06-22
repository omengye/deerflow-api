"""OpenAI-compatible streaming chat completions endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.dependencies import get_client_manager
from app.middleware import get_request_id
from deerflow.runtime import ConflictError, END_SENTINEL, HEARTBEAT_SENTINEL, RunStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["openai-compatible"])


class OpenAIChatMessage(BaseModel):
    role: str
    content: Any = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class OpenAIChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[OpenAIChatMessage] = Field(min_length=1)
    stream: bool = True
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    user: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, req: OpenAIChatCompletionRequest = Body()):
    """Stream DeerFlow output using the OpenAI chat completions SSE shape."""
    if req.stream is not True:
        raise HTTPException(status_code=501, detail="Only stream=true is supported")

    message = _latest_user_message(req)
    if message is None:
        raise HTTPException(status_code=400, detail="No user message found in request")

    _log_request_tools(req)

    manager = get_client_manager()
    thread_id = _thread_id_from_request(req)
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    model = req.model or "deerflow"
    kwargs: dict[str, Any] = {}
    if req.model:
        kwargs["model_name"] = req.model

    try:
        record = await manager.start_client_stream_run(
            thread_id=thread_id,
            message=message,
            kwargs=kwargs,
            request_id=get_request_id(),
            entrypoint="openai_chat_completions",
            on_disconnect="cancel",
            multitask_strategy="reject",
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    last_event_id = request.headers.get("last-event-id")

    async def event_generator():
        role_sent = False
        reasoning_content_state: dict[str, str] = {}
        try:
            yield _sse(_chunk(completion_id, created, model, delta={"role": "assistant"}))
            role_sent = True

            async for entry in manager.stream_bridge.subscribe(record.run_id, last_event_id=last_event_id):
                if entry is HEARTBEAT_SENTINEL:
                    continue
                if entry is END_SENTINEL:
                    break

                if entry.event == "messages-tuple":
                    data = _unwrap_message_tuple(entry.data)
                    if not isinstance(data, dict):
                        continue
                    for payload in _message_tuple_to_openai_chunks(completion_id, created, model, data, reasoning_content_state):
                        yield _sse(payload)
                elif entry.event == "tool_call_chunk":
                    # DeerFlow tool calls are executed internally by the backend.
                    # Emitting them as OpenAI Chat Completions `tool_calls` would
                    # make compatible clients think they must execute external tools.
                    continue
                elif entry.event == "thinking_chunk":
                    if isinstance(entry.data, dict):
                        payload = _thinking_chunk_to_openai(completion_id, created, model, entry.data)
                        if payload is not None:
                            yield _sse(payload)
                elif entry.event == "error":
                    error = "Internal server error"
                    if isinstance(entry.data, dict) and entry.data.get("error"):
                        error = str(entry.data["error"])
                    yield _sse({"error": {"message": error, "type": "deerflow_error"}})

            final_record = manager.run_manager.get(record.run_id)
            finish_reason = "stop"
            if final_record is not None and final_record.status in {RunStatus.error, RunStatus.timeout, RunStatus.interrupted}:
                finish_reason = "error"
            if not role_sent:
                yield _sse(_chunk(completion_id, created, model, delta={"role": "assistant"}))
            yield _sse(_chunk(completion_id, created, model, delta={}, finish_reason=finish_reason))
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            await manager.cancel_run(record.run_id)
            raise
        except Exception:
            logger.exception("Unhandled error in /v1/chat/completions (thread=%s)", thread_id)
            yield _sse({"error": {"message": "Internal server error", "type": "deerflow_error"}})
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _latest_user_message(req: OpenAIChatCompletionRequest) -> str | None:
    for message in reversed(req.messages):
        if message.role == "user":
            content = _content_to_text(message.content)
            if content:
                return content
    return None


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


def _thread_id_from_request(req: OpenAIChatCompletionRequest) -> str:
    if req.user:
        return f"openai_{req.user[:96]}"
    return str(uuid.uuid4())


def _log_request_tools(req: OpenAIChatCompletionRequest) -> None:
    if not req.tools:
        return
    names: list[str] = []
    for tool in req.tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function["name"]))
    logger.info(
        "OpenAI-compatible request included tools; logging only, not injecting into DeerFlow: count=%d names=%s tool_choice=%s",
        len(req.tools),
        names,
        req.tool_choice,
    )


def _unwrap_message_tuple(data: Any) -> Any:
    if isinstance(data, list) and data:
        return data[0]
    return data


def _message_tuple_to_openai_chunks(
    completion_id: str,
    created: int,
    model: str,
    data: dict[str, Any],
    reasoning_content_state: dict[str, str],
) -> list[dict[str, Any]]:
    message_type = data.get("type")
    if message_type in ("AIMessageChunk", "AIMessage"):
        message_type = "ai"
    if message_type == "ai":
        chunks: list[dict[str, Any]] = []
        reasoning = data.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            reasoning_id = str(data.get("id") or "default")
            delta = _reasoning_delta(reasoning_content_state, reasoning_id, reasoning)
            if delta:
                chunks.append(_chunk(completion_id, created, model, delta={"reasoning_content": delta}))
        content = data.get("content")
        if isinstance(content, str) and content:
            chunks.append(_chunk(completion_id, created, model, delta={"content": content}))
        return chunks
    return []


def _thinking_chunk_to_openai(
    completion_id: str,
    created: int,
    model: str,
    data: dict[str, Any],
) -> dict[str, Any] | None:
    thinking = data.get("thinking")
    if not isinstance(thinking, str) or not thinking:
        return None
    return _chunk(completion_id, created, model, delta={"reasoning_content": thinking})


def _reasoning_delta(reasoning_content_state: dict[str, str], reasoning_id: str, reasoning: str) -> str:
    previous = reasoning_content_state.get(reasoning_id, "")
    if previous and reasoning.startswith(previous):
        delta = reasoning[len(previous):]
    else:
        delta = reasoning
    reasoning_content_state[reasoning_id] = reasoning if reasoning.startswith(previous) else previous + delta
    return delta


def _chunk(
    completion_id: str,
    created: int,
    model: str,
    *,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
