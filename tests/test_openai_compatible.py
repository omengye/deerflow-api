import asyncio
import json
import unittest
from collections.abc import AsyncIterator, Iterable
from typing import Any, cast

from fastapi import Request

from app.routers import openai_compatible
from app.routers.openai_compatible import OpenAIChatCompletionRequest
from deerflow.client import StreamEvent, StreamEventType
from deerflow.runtime import DisconnectMode, MemoryStreamBridge, RunManager, RunStatus


class _FakeClient:
    def __init__(self, events: Iterable[StreamEvent]) -> None:
        self.agent_name = "lead_agent"
        self.events = list(events)
        self.last_message: str | None = None

    async def astream(self, *args: object, **_kwargs: object) -> AsyncIterator[StreamEvent]:
        self.last_message = str(args[0]) if args else None
        for event in self.events:
            yield event


class _FakeRequest:
    headers: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return False


def _fake_request() -> Request:
    return cast(Request, cast(object, _FakeRequest()))


def _event_type(value: str) -> StreamEventType:
    return cast(StreamEventType, cast(object, value))


class _FakeManager:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client
        self.run_manager = RunManager()
        self.stream_bridge = MemoryStreamBridge(queue_maxsize=32)

    async def start_client_stream_run(
        self,
        *,
        thread_id: str,
        message: str,
        kwargs: dict[str, object],
        request_id: str | None = None,
        run_id: str | None = None,
        entrypoint: str = "chat_stream",
        on_disconnect: str = "cancel",
        multitask_strategy: str = "reject",
    ):
        record = await self.run_manager.create_or_reject(
            thread_id,
            run_id=run_id,
            on_disconnect=DisconnectMode.cancel if on_disconnect == "cancel" else DisconnectMode.continue_,
            multitask_strategy=multitask_strategy,
            metadata={"request_id": request_id, "entrypoint": entrypoint},
            kwargs=kwargs,
        )

        async def produce() -> None:
            try:
                await self.run_manager.set_status(record.run_id, RunStatus.running)
                await self.stream_bridge.publish(record.run_id, "metadata", {"run_id": record.run_id, "thread_id": thread_id})
                async for event in self.client.astream(message, thread_id=thread_id):
                    await self.stream_bridge.publish(record.run_id, event.type, event.data)
                await self.run_manager.set_status(record.run_id, RunStatus.success)
            finally:
                await self.stream_bridge.publish_end(record.run_id)

        record.task = asyncio.create_task(produce())
        return record

    async def cancel_run(self, run_id: str, *, action: str = "interrupt") -> bool:
        return await self.run_manager.cancel(run_id, action=action)


def _request(payload: dict[str, object]) -> OpenAIChatCompletionRequest:
    return OpenAIChatCompletionRequest.model_validate(payload)


async def _collect(response) -> list[str]:
    return [str(chunk) async for chunk in response.body_iterator]


def _data(chunk: str) -> dict[str, Any] | str:
    assert chunk.startswith("data: ")
    raw = chunk.removeprefix("data: ").strip()
    if raw == "[DONE]":
        return raw
    return json.loads(raw)


class OpenAICompatibleTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, fake_client: _FakeClient, payload: dict[str, object]) -> tuple[list[dict[str, Any] | str], _FakeClient]:
        fake_manager = _FakeManager(fake_client)
        original = openai_compatible.get_client_manager
        openai_compatible.get_client_manager = lambda: fake_manager
        try:
            response = await openai_compatible.chat_completions(_fake_request(), _request(payload))
            chunks = await _collect(response)
        finally:
            openai_compatible.get_client_manager = original
        return [_data(chunk) for chunk in chunks], fake_client

    async def test_streams_text_as_openai_chunks(self) -> None:
        events = [
            StreamEvent(type="messages-tuple", data={"type": "ai", "content": "hello", "id": "msg-1"}),
            StreamEvent(type="messages-tuple", data={"type": "ai", "content": " world", "id": "msg-1"}),
            StreamEvent(type="end", data={"usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}),
        ]

        chunks, fake_client = await self._run(
            _FakeClient(events),
            {"model": "model-a", "stream": True, "messages": [{"role": "user", "content": "say hi"}]},
        )

        self.assertEqual(fake_client.last_message, "say hi")
        self.assertEqual(cast(dict[str, Any], chunks[0])["choices"][0]["delta"], {"role": "assistant"})
        self.assertEqual(cast(dict[str, Any], chunks[1])["choices"][0]["delta"], {"content": "hello"})
        self.assertEqual(cast(dict[str, Any], chunks[2])["choices"][0]["delta"], {"content": " world"})
        self.assertEqual(cast(dict[str, Any], chunks[-2])["choices"][0]["finish_reason"], "stop")
        self.assertEqual(chunks[-1], "[DONE]")

    async def test_logs_request_tools_without_injecting_them(self) -> None:
        events = [StreamEvent(type="end", data={"usage": {}})]
        payload: dict[str, object] = {
            "model": "model-a",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
            "tool_choice": "auto",
        }

        with self.assertLogs(openai_compatible.logger, level="INFO") as logs:
            chunks, _fake_client = await self._run(_FakeClient(events), payload)

        self.assertIn("logging only, not injecting", "\n".join(logs.output))
        self.assertEqual(cast(dict[str, Any], chunks[-2])["choices"][0]["finish_reason"], "stop")

    async def test_suppresses_internal_message_tool_calls_in_openai_chunks(self) -> None:
        events = [
            StreamEvent(
                type="messages-tuple",
                data={"type": "ai", "content": "", "id": "msg-1", "tool_calls": [{"name": "search", "args": {"q": "deer"}, "id": "call-1"}]},
            ),
            StreamEvent(type="end", data={"usage": {}}),
        ]

        chunks, _fake_client = await self._run(
            _FakeClient(events),
            {"model": "model-a", "stream": True, "messages": [{"role": "user", "content": "search"}]},
        )

        for chunk in chunks:
            if isinstance(chunk, dict):
                delta = chunk["choices"][0]["delta"]
                self.assertNotIn("tool_calls", delta)

    async def test_suppresses_internal_tool_call_chunk_events_in_openai_chunks(self) -> None:
        events = [
            StreamEvent(type=_event_type("tool_call_chunk"), data={"tool_call": {"name": "lookup", "args": {"id": 7}, "id": "call-7"}}),
            StreamEvent(type="end", data={"usage": {}}),
        ]

        chunks, _fake_client = await self._run(
            _FakeClient(events),
            {"model": "model-a", "stream": True, "messages": [{"role": "user", "content": "lookup"}]},
        )

        for chunk in chunks:
            if isinstance(chunk, dict):
                delta = chunk["choices"][0]["delta"]
                self.assertNotIn("tool_calls", delta)

    async def test_maps_reasoning_content_to_openai_chunks_incrementally(self) -> None:
        events = [
            StreamEvent(type="messages-tuple", data={"type": "ai", "content": "", "id": "msg-1", "reasoning_content": "think"}),
            StreamEvent(type="messages-tuple", data={"type": "ai", "content": "", "id": "msg-1", "reasoning_content": "thinking"}),
            StreamEvent(type="messages-tuple", data={"type": "ai", "content": "answer", "id": "msg-1"}),
            StreamEvent(type="end", data={"usage": {}}),
        ]

        chunks, _fake_client = await self._run(
            _FakeClient(events),
            {"model": "model-a", "stream": True, "messages": [{"role": "user", "content": "think"}]},
        )

        self.assertEqual(cast(dict[str, Any], chunks[1])["choices"][0]["delta"], {"reasoning_content": "think"})
        self.assertEqual(cast(dict[str, Any], chunks[2])["choices"][0]["delta"], {"reasoning_content": "ing"})
        self.assertEqual(cast(dict[str, Any], chunks[3])["choices"][0]["delta"], {"content": "answer"})

    async def test_maps_subagent_thinking_chunks_to_openai_reasoning_content(self) -> None:
        events = [
            StreamEvent(type=_event_type("thinking_chunk"), data={"task_id": "task-1", "thinking": "sub-think"}),
            StreamEvent(type="end", data={"usage": {}}),
        ]

        chunks, _fake_client = await self._run(
            _FakeClient(events),
            {"model": "model-a", "stream": True, "messages": [{"role": "user", "content": "delegate"}]},
        )

        self.assertEqual(cast(dict[str, Any], chunks[1])["choices"][0]["delta"], {"reasoning_content": "sub-think"})

    async def test_rejects_non_streaming_requests(self) -> None:
        with self.assertRaises(Exception) as cm:
            await openai_compatible.chat_completions(
                _fake_request(),
                _request({"model": "model-a", "stream": False, "messages": [{"role": "user", "content": "hello"}]}),
            )
        self.assertEqual(getattr(cm.exception, "status_code"), 501)


if __name__ == "__main__":
    unittest.main()
