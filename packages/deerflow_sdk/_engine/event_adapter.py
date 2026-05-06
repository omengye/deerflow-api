from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from deerflow_sdk._events import RunComplete, StreamEvent, TextDelta, TokenUsage, ToolCall, ToolResult


@dataclass
class StreamState:
    seen_ids: set[str] = field(default_factory=set)
    streamed_ids: set[str] = field(default_factory=set)
    emitted_tool_call_ids: set[str] = field(default_factory=set)
    emitted_tool_result_ids: set[str] = field(default_factory=set)
    counted_usage_ids: set[str] = field(default_factory=set)
    cumulative_usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    text_chunks_by_id: dict[str, list[str]] = field(default_factory=dict)
    last_ai_id: str = ""
    final_output: str = ""
    reasoning_output: str = ""


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if content and all(isinstance(block, str) for block in content):
            chunk_like = len(content) > 1 and all(len(block) <= 20 and any(ch in block for ch in '{}[]":,') for block in content)
            return "".join(content) if chunk_like else "\n".join(content)

        pieces: list[str] = []
        pending: list[str] = []
        for block in content:
            if isinstance(block, str):
                pending.append(block)
            elif isinstance(block, dict):
                if pending:
                    pieces.append("".join(pending))
                    pending.clear()
                text = block.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        if pending:
            pieces.append("".join(pending))
        return "\n".join(pieces) if pieces else ""
    return str(content)


def events_from_stream_item(item: Any, state: StreamState) -> Iterable[StreamEvent]:
    from langchain_core.messages import AIMessage, ToolMessage

    mode: str
    chunk: Any
    if isinstance(item, tuple) and len(item) == 2:
        mode, chunk = str(item[0]), item[1]
    else:
        mode, chunk = "values", item

    if mode == "messages":
        msg_chunk = chunk[0] if isinstance(chunk, tuple) and len(chunk) == 2 else chunk
        msg_id = getattr(msg_chunk, "id", None)
        if isinstance(msg_chunk, AIMessage):
            text = extract_text(msg_chunk.content)
            _account_usage(state, msg_id, getattr(msg_chunk, "usage_metadata", None))

            reasoning = msg_chunk.additional_kwargs.get("reasoning_content") or msg_chunk.additional_kwargs.get("reasoning")
            if reasoning and isinstance(reasoning, str):
                state.reasoning_output += reasoning
                yield TextDelta(delta=reasoning)

            if text:
                if msg_id:
                    state.streamed_ids.add(msg_id)
                _record_ai_text(state, msg_id, text, append=True)
                yield TextDelta(delta=text)

            for event in _invalid_tool_call_events(msg_chunk, state):
                yield event
        elif isinstance(msg_chunk, ToolMessage):
            if msg_id:
                state.streamed_ids.add(msg_id)
            tool_result = _tool_result_event(msg_chunk, state)
            if tool_result is not None:
                yield tool_result
        return

    if mode != "values" or not isinstance(chunk, dict):
        return

    messages = chunk.get("messages", [])
    for msg in messages:
        msg_id = getattr(msg, "id", None)
        if msg_id and msg_id in state.seen_ids:
            continue
        if msg_id:
            state.seen_ids.add(msg_id)

        if isinstance(msg, AIMessage):
            _account_usage(state, msg_id, getattr(msg, "usage_metadata", None))
            reasoning = msg.additional_kwargs.get("reasoning_content") or msg.additional_kwargs.get("reasoning")
            if reasoning and isinstance(reasoning, str):
                state.reasoning_output += reasoning
                yield TextDelta(delta=reasoning)

            for tool_call_event in _tool_call_events(msg, state):
                yield tool_call_event
            for invalid_tool_call_event in _invalid_tool_call_events(msg, state):
                yield invalid_tool_call_event
            text = extract_text(msg.content)
            if text:
                _record_ai_text(state, msg_id, text, append=False)
            if not (msg_id and msg_id in state.streamed_ids):
                if text:
                    yield TextDelta(delta=text)
        elif isinstance(msg, ToolMessage):
            tool_result = _tool_result_event(msg, state)
            if tool_result is not None:
                yield tool_result


def complete_event(state: StreamState, final_output: Any | None = None) -> RunComplete:
    return RunComplete(
        final_output=state.final_output if final_output is None else final_output,
        usage=TokenUsage(**state.cumulative_usage),
    )


def _tool_call_events(msg: Any, state: StreamState) -> Iterable[ToolCall]:
    for tool_call in getattr(msg, "tool_calls", None) or []:
        call_id = str(tool_call.get("id") or "")
        if call_id and call_id in state.emitted_tool_call_ids:
            continue
        if call_id:
            state.emitted_tool_call_ids.add(call_id)
        args = tool_call.get("args") or {}
        yield ToolCall(
            tool_name=str(tool_call.get("name") or ""),
            tool_call_id=call_id,
            input=args if isinstance(args, dict) else {"value": args},
        )


def _invalid_tool_call_events(msg: Any, state: StreamState) -> Iterable[ToolResult]:
    for tool_call in getattr(msg, "invalid_tool_calls", None) or []:
        call_id = str(tool_call.get("id") or "")
        if call_id and call_id in state.emitted_tool_call_ids:
            continue
        if call_id:
            state.emitted_tool_call_ids.add(call_id)
        yield ToolResult(
            tool_call_id=call_id,
            tool_name=str(tool_call.get("name") or ""),
            error=str(tool_call.get("error") or "malformed tool call"),
        )


def _tool_result_event(msg: Any, state: StreamState) -> ToolResult | None:
    msg_id = str(getattr(msg, "id", "") or "")
    result_id = msg_id or str(getattr(msg, "tool_call_id", "") or "")
    if result_id and result_id in state.emitted_tool_result_ids:
        return None
    if result_id:
        state.emitted_tool_result_ids.add(result_id)
    status = getattr(msg, "status", None)
    content = extract_text(getattr(msg, "content", ""))
    return ToolResult(
        tool_call_id=str(getattr(msg, "tool_call_id", "") or ""),
        tool_name=str(getattr(msg, "name", "") or ""),
        output=None if status == "error" else content,
        error=content if status == "error" else None,
    )


def _account_usage(state: StreamState, msg_id: str | None, usage: Any) -> None:
    if not usage:
        return
    if msg_id and msg_id in state.counted_usage_ids:
        return
    if msg_id:
        state.counted_usage_ids.add(msg_id)
    state.cumulative_usage["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
    state.cumulative_usage["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
    state.cumulative_usage["total_tokens"] += int(usage.get("total_tokens", 0) or 0)


def _record_ai_text(state: StreamState, msg_id: str | None, text: str, *, append: bool) -> None:
    key = msg_id or ""
    state.last_ai_id = key
    if append:
        state.text_chunks_by_id.setdefault(key, []).append(text)
    else:
        state.text_chunks_by_id[key] = [text]
    state.final_output = "".join(state.text_chunks_by_id.get(state.last_ai_id, ()))
