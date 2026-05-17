import asyncio
import json
import unittest
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Any, cast
from unittest.mock import patch

from fastapi import Request

from app.dependencies import ClientManager
from app.routers import chat
from app.schemas import AguiRunAgentInput, ChatRequest
from deerflow.client import DeerFlowClient, StreamEvent, StreamEventType
from deerflow.runtime import DisconnectMode, MemoryStreamBridge, RunManager, RunStatus
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.messages import AIMessageChunk
from langchain_core.runnables import RunnableConfig


class _FakeClient:
    def __init__(self, events: Iterable[StreamEvent] | None = None, error_after_first: bool = False) -> None:
        self.agent_name: str = "lead_agent"
        self.astream_called: bool = False
        self.stream_called: bool = False
        self.last_message: str | None = None
        self.events: list[StreamEvent] = list(
            events
            if events is not None
            else [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "hello", "id": "msg-1"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}),
            ]
        )
        self.error_after_first: bool = error_after_first

    def stream(self, *_args: object, **_kwargs: object) -> Iterator[StreamEvent]:
        self.stream_called = True
        raise AssertionError("chat_stream must use async DeerFlowClient.astream(), not sync stream()")

    async def astream(self, *args: object, **_kwargs: object) -> AsyncIterator[StreamEvent]:
        self.astream_called = True
        self.last_message = str(args[0]) if args else None
        for index, event in enumerate(self.events):
            yield event
            if self.error_after_first and index == 0:
                raise RuntimeError("boom")


class _FakeRequest:
    headers: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return False


def _fake_request() -> Request:
    return cast(Request, cast(object, _FakeRequest()))


def _stream_event_type(value: str) -> StreamEventType:
    return cast(StreamEventType, cast(object, value))


class _FakeManager:
    def __init__(self, client: _FakeClient) -> None:
        self.client: _FakeClient = client
        self.running: list[str] = []
        self.done: list[str] = []
        self.run_manager = RunManager()
        self.stream_bridge = MemoryStreamBridge(queue_maxsize=32)

    def get_client(self) -> _FakeClient:
        return self.client

    async def get_async_client(self, **kwargs: Any) -> _FakeClient:
        return self.client

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
        self.mark_thread_running(thread_id)

        async def produce() -> None:
            try:
                await self.run_manager.set_status(record.run_id, RunStatus.running)
                await self.stream_bridge.publish(record.run_id, "metadata", {"run_id": record.run_id, "thread_id": thread_id})
                async for event in self.client.astream(message, thread_id=thread_id):
                    await self.stream_bridge.publish(record.run_id, event.type, event.data)
                await self.run_manager.set_status(record.run_id, RunStatus.success)
            except Exception as exc:
                await self.stream_bridge.publish(record.run_id, "error", {"error": str(exc)})
                await self.run_manager.set_status(record.run_id, RunStatus.error, error=str(exc))
            finally:
                self.mark_thread_done(thread_id)
                await self.stream_bridge.publish_end(record.run_id)

        record.task = asyncio.create_task(produce())
        return record

    async def cancel_run(self, run_id: str, *, action: str = "interrupt") -> bool:
        return await self.run_manager.cancel(run_id, action=action)

    def mark_thread_running(self, thread_id: str) -> None:
        self.running.append(thread_id)

    def mark_thread_done(self, thread_id: str) -> None:
        self.done.append(thread_id)


class ChatStreamingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _agui_request(parent_run_id: str | None = None) -> AguiRunAgentInput:
        payload: dict[str, object] = {
            "threadId": "thread-1",
            "runId": "run-1",
            "state": {},
            "messages": [{"id": "user-1", "role": "user", "content": "hello"}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
        if parent_run_id:
            payload["parentRunId"] = parent_run_id
        return AguiRunAgentInput.model_validate(
            payload
        )

    async def _collect_agui_stream(self, fake_client: _FakeClient, parent_run_id: str | None = None) -> tuple[list[dict[str, object]], _FakeManager, list[str]]:
        fake_manager = _FakeManager(fake_client)
        original_get_client_manager = chat.get_client_manager
        chat.get_client_manager = lambda: fake_manager
        try:
            response = await chat.chat_agui(_fake_request(), self._agui_request(parent_run_id=parent_run_id))
            chunks = [str(chunk) async for chunk in response.body_iterator]
        finally:
            chat.get_client_manager = original_get_client_manager
        events: list[dict[str, object]] = []
        for chunk in chunks:
            self.assertTrue(chunk.startswith("data: "))
            self.assertTrue(chunk.endswith("\n\n"))
            events.append(json.loads(chunk.removeprefix("data: ").strip()))
        return events, fake_manager, chunks

    async def test_chat_stream_options_preflight(self) -> None:
        response = await chat.chat_stream_options()

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["allow"], "OPTIONS, POST")
        self.assertEqual(response.headers["access-control-allow-origin"], "*")
        self.assertEqual(response.headers["access-control-allow-methods"], "OPTIONS, POST")
        self.assertEqual(response.headers["access-control-allow-headers"], "Content-Type, Authorization")

    async def test_chat_agui_options_preflight(self) -> None:
        response = await chat.chat_agui_options()

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["allow"], "OPTIONS, POST")
        self.assertEqual(response.headers["access-control-allow-methods"], "OPTIONS, POST")
        self.assertIn("Accept", response.headers["access-control-allow-headers"])

    async def test_chat_agui_disables_sse_buffering(self) -> None:
        response = await chat.chat_agui(_fake_request(), self._agui_request())

        self.assertEqual(response.headers["cache-control"], "no-cache, no-transform")
        self.assertEqual(response.headers["x-accel-buffering"], "no")

    async def _collect_chat_stream(self, fake_client: _FakeClient) -> tuple[list[dict[str, str]], _FakeManager]:
        fake_manager = _FakeManager(fake_client)
        original_get_client_manager = chat.get_client_manager
        chat.get_client_manager = lambda: fake_manager
        try:
            response = await chat.chat_stream(_fake_request(), ChatRequest(message="hello", thread_id="thread-1"))
            chunks = [cast(dict[str, str], chunk) async for chunk in response.body_iterator]
        finally:
            chat.get_client_manager = original_get_client_manager
        return chunks, fake_manager

    async def test_chat_stream_endpoint_forwards_request_options(self) -> None:
        fake_client = _FakeClient()
        fake_manager = _FakeManager(fake_client)
        original_get_client_manager = chat.get_client_manager
        chat.get_client_manager = lambda: fake_manager
        try:
            response = await chat.chat_stream(
                _fake_request(),
                ChatRequest(
                    message="hello",
                    thread_id="thread-1",
                    model_name="model-a",
                    thinking_enabled=False,
                    subagent_enabled=True,
                    plan_mode=True,
                    max_concurrent_subagents=4,
                    multitask_strategy="interrupt",
                    on_disconnect="continue",
                ),
            )
            chunks = [cast(dict[str, str], chunk) async for chunk in response.body_iterator]
        finally:
            chat.get_client_manager = original_get_client_manager

        metadata = json.loads(chunks[0]["data"])
        record = fake_manager.run_manager.get(metadata["run_id"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.thread_id, "thread-1")
        self.assertEqual(record.on_disconnect, "continue")
        self.assertEqual(record.multitask_strategy, "interrupt")
        self.assertEqual(
            record.kwargs,
            {
                "model_name": "model-a",
                "thinking_enabled": False,
                "subagent_enabled": True,
                "plan_mode": True,
                "max_concurrent_subagents": 4,
            },
        )

    async def test_chat_agui_endpoint_uses_latest_user_message_and_options(self) -> None:
        fake_client = _FakeClient()
        fake_manager = _FakeManager(fake_client)
        original_get_client_manager = chat.get_client_manager
        chat.get_client_manager = lambda: fake_manager
        payload = {
            "threadId": "thread-1",
            "runId": "run-1",
            "state": {},
            "messages": [
                {"id": "user-1", "role": "user", "content": "older"},
                {"id": "assistant-1", "role": "assistant", "content": "assistant"},
                {"id": "user-2", "role": "user", "content": "newest"},
            ],
            "tools": [],
            "context": [],
            "forwardedProps": {},
            "modelName": "model-a",
            "thinkingEnabled": False,
            "subagentEnabled": True,
            "planMode": True,
            "maxConcurrentSubagents": 4,
            "agentName": "agent-a",
            "multitaskStrategy": "rollback",
            "onDisconnect": "continue",
        }
        try:
            response = await chat.chat_agui(_fake_request(), AguiRunAgentInput.model_validate(payload))
            chunks = [str(chunk) async for chunk in response.body_iterator]
        finally:
            chat.get_client_manager = original_get_client_manager

        events = [json.loads(chunk.removeprefix("data: ").strip()) for chunk in chunks]
        self.assertEqual(events[0], {"type": "RUN_STARTED", "threadId": "thread-1", "runId": "run-1", "rawEvent": {"name": "lead_agent"}})
        record = fake_manager.run_manager.get("run-1")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.thread_id, "thread-1")
        self.assertEqual(record.on_disconnect, "continue")
        self.assertEqual(record.multitask_strategy, "rollback")
        self.assertEqual(record.kwargs["model_name"], "model-a")
        self.assertEqual(record.kwargs["subagent_enabled"], True)
        self.assertTrue(fake_client.astream_called)
        self.assertEqual(fake_client.last_message, "newest")

    async def test_deerflow_client_astream_uses_agent_astream(self) -> None:
        class FakeAgent:
            def __init__(self) -> None:
                self.astream_called: bool = False
                self.stream_called: bool = False
                self.stream_modes: list[object] = []

            def stream(self, *_args: object, **_kwargs: object) -> Iterator[dict[str, object]]:
                self.stream_called = True
                raise AssertionError("DeerFlowClient.astream must not call sync agent.stream()")

            async def astream(self, *_args: object, **_kwargs: object) -> AsyncIterator[tuple[str, tuple[AIMessageChunk, dict[str, object]]]]:
                self.astream_called = True
                self.stream_modes.append(_kwargs.get("stream_mode"))
                yield "messages", (AIMessageChunk(content="hello", id="msg-1"), {})

        class TestClient(DeerFlowClient):
            def _prepare_stream_invocation(
                self,
                message: str,
                thread_id: str | None,
                **kwargs: object,
            ) -> tuple[RunnableConfig, dict[str, object], dict[str, object]]:
                return RunnableConfig(), {"messages": [message]}, {"thread_id": thread_id or "generated"}

        fake_agent = FakeAgent()
        client = object.__new__(TestClient)
        object.__setattr__(client, "_agent", fake_agent)
        object.__setattr__(client, "_agent_name", None)

        events = [event async for event in client.astream("hello", thread_id="thread-1")]

        self.assertTrue(fake_agent.astream_called)
        self.assertFalse(fake_agent.stream_called)
        self.assertEqual(fake_agent.stream_modes, [["values", "messages", "custom"]])
        self.assertEqual(events[0], StreamEvent(type="messages-tuple", data={"type": "ai", "content": "hello", "id": "msg-1", "is_delta": True}))
        self.assertEqual(events[-1], StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}))

    async def test_deerflow_client_stream_and_astream_emit_same_events(self) -> None:
        stream_items: list[tuple[str, object]] = [
            ("messages", (AIMessageChunk(content="hello", id="msg-1"), {})),
            ("custom", {"progress": "working"}),
            ("values", {"title": "Chat", "messages": [], "artifacts": []}),
        ]

        class FakeAgent:
            def stream(self, *_args: object, **_kwargs: object) -> Iterator[tuple[str, object]]:
                yield from stream_items

            async def astream(self, *_args: object, **_kwargs: object) -> AsyncIterator[tuple[str, object]]:
                for item in stream_items:
                    yield item

        class TestClient(DeerFlowClient):
            def _prepare_stream_invocation(
                self,
                message: str,
                thread_id: str | None,
                **kwargs: object,
            ) -> tuple[RunnableConfig, dict[str, object], dict[str, object]]:
                return RunnableConfig(), {"messages": [message]}, {"thread_id": thread_id or "generated"}

        client = object.__new__(TestClient)
        object.__setattr__(client, "_agent", FakeAgent())
        object.__setattr__(client, "_agent_name", None)

        sync_events = list(client.stream("hello", thread_id="thread-1"))
        async_events = [event async for event in client.astream("hello", thread_id="thread-1")]

        self.assertEqual(async_events, sync_events)

    async def test_chat_stream_uses_async_client_stream(self) -> None:
        fake_client = _FakeClient()
        chunks, fake_manager = await self._collect_chat_stream(fake_client)

        self.assertTrue(fake_client.astream_called)
        self.assertFalse(fake_client.stream_called)
        self.assertEqual(fake_manager.running, ["thread-1"])
        self.assertEqual(fake_manager.done, ["thread-1"])

        self.assertEqual(chunks[0]["event"], "metadata")
        first_event = chunks[1]
        second_event = chunks[2]
        third_event = chunks[3]
        self.assertEqual(first_event["event"], "messages-tuple")
        self.assertEqual(json.loads(first_event["data"]), {"type": "ai", "content": "hello", "id": "msg-1"})
        self.assertEqual(second_event["event"], "text")
        self.assertEqual(json.loads(second_event["data"]), {"content": "hello", "thread_id": "thread-1", "run_id": json.loads(chunks[0]["data"])["run_id"]})
        self.assertEqual(third_event["event"], "end")

    async def test_chat_stream_does_not_emit_text_event_for_tool_calls(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "", "id": "msg-1", "tool_calls": [{"name": "tool", "args": {}, "id": "call-1"}]}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        chunks, _fake_manager = await self._collect_chat_stream(fake_client)

        self.assertEqual([chunk["event"] for chunk in chunks], ["metadata", "messages-tuple", "end"])

    async def test_chat_stream_emits_error_and_cleans_up_after_midstream_failure(self) -> None:
        fake_client = _FakeClient(error_after_first=True)

        chunks, fake_manager = await self._collect_chat_stream(fake_client)

        self.assertEqual([chunk["event"] for chunk in chunks], ["metadata", "messages-tuple", "text", "error"])
        self.assertEqual(json.loads(chunks[-1]["data"]), {"error": "boom"})
        self.assertEqual(fake_manager.done, ["thread-1"])

    async def test_chat_agui_emits_protocol_lifecycle_and_text_events(self) -> None:
        fake_client = _FakeClient()

        events, fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual([event["type"] for event in events], ["RUN_STARTED", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "CUSTOM", "RUN_FINISHED"])
        self.assertEqual(events[0], {"type": "RUN_STARTED", "threadId": "thread-1", "runId": "run-1", "rawEvent": {"name": "lead_agent"}})
        self.assertEqual(events[1], {"type": "TEXT_MESSAGE_START", "messageId": "msg-1", "role": "assistant", "rawEvent": {"name": "lead_agent"}})
        self.assertEqual(events[2], {"type": "TEXT_MESSAGE_CONTENT", "messageId": "msg-1", "delta": "hello", "rawEvent": {"name": "lead_agent"}})
        self.assertEqual(events[3], {"type": "TEXT_MESSAGE_END", "messageId": "msg-1", "rawEvent": {"name": "lead_agent"}})
        self.assertEqual(events[-1], {"type": "RUN_FINISHED", "threadId": "thread-1", "runId": "run-1", "rawEvent": {"name": "lead_agent"}})
        self.assertEqual(fake_manager.done, ["thread-1"])

    async def test_chat_agui_keeps_reasoning_messages_separate_by_id(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "", "id": "msg-1", "reasoning_content": "think"}),
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "", "id": "msg-2", "reasoning_content": "plan"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)
        reasoning_events = [event for event in events if str(event["type"]).startswith("REASONING_MESSAGE_")]

        self.assertEqual(
            reasoning_events,
            [
                {"type": "REASONING_MESSAGE_START", "messageId": "reasoning_msg-1", "role": "reasoning", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_CONTENT", "messageId": "reasoning_msg-1", "delta": "think", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_START", "messageId": "reasoning_msg-2", "role": "reasoning", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_CONTENT", "messageId": "reasoning_msg-2", "delta": "plan", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_END", "messageId": "reasoning_msg-1", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_END", "messageId": "reasoning_msg-2", "rawEvent": {"name": "lead_agent"}},
            ],
        )

    async def test_chat_agui_emits_incremental_reasoning_delta_for_cumulative_chunks(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "", "id": "msg-1", "reasoning_content": "think"}),
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "", "id": "msg-1", "reasoning_content": "thinking"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)
        reasoning_content_events = [event for event in events if event["type"] == "REASONING_MESSAGE_CONTENT"]

        self.assertEqual(
            reasoning_content_events,
            [
                {"type": "REASONING_MESSAGE_CONTENT", "messageId": "reasoning_msg-1", "delta": "think", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_CONTENT", "messageId": "reasoning_msg-1", "delta": "ing", "rawEvent": {"name": "lead_agent"}},
            ],
        )

    async def test_chat_agui_closes_same_chunk_reasoning_before_text(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "answer", "id": "msg-1", "reasoning_content": "think"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual(
            [event for event in events if event["type"] in {"REASONING_MESSAGE_START", "REASONING_MESSAGE_CONTENT", "REASONING_MESSAGE_END", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT"}],
            [
                {"type": "REASONING_MESSAGE_START", "messageId": "reasoning_msg-1", "role": "reasoning", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_CONTENT", "messageId": "reasoning_msg-1", "delta": "think", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_END", "messageId": "reasoning_msg-1", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_START", "messageId": "msg-1", "role": "assistant", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "msg-1", "delta": "answer", "rawEvent": {"name": "lead_agent"}},
            ],
        )

    async def test_chat_agui_closes_only_current_reasoning_id_before_text(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "", "id": "msg-1", "reasoning_content": "first"}),
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "", "id": "msg-2", "reasoning_content": "second"}),
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "done", "id": "msg-1"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)
        reasoning_ends = [event for event in events if event["type"] == "REASONING_MESSAGE_END"]

        self.assertEqual(
            reasoning_ends,
            [
                {"type": "REASONING_MESSAGE_END", "messageId": "reasoning_msg-1", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_END", "messageId": "reasoning_msg-2", "rawEvent": {"name": "lead_agent"}},
            ],
        )
        self.assertLess(events.index(reasoning_ends[0]), next(index for index, event in enumerate(events) if event["type"] == "TEXT_MESSAGE_START"))
        self.assertGreater(events.index(reasoning_ends[1]), next(index for index, event in enumerate(events) if event["type"] == "TEXT_MESSAGE_CONTENT"))

    async def test_chat_agui_maps_tool_calls_and_results(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "", "id": "msg-1", "tool_calls": [{"name": "search", "args": {"q": "x"}, "id": "call-1"}]}),
                StreamEvent(type="messages-tuple", data={"type": "tool", "content": "result", "name": "search", "tool_call_id": "call-1", "id": "tool-msg-1"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual([event["type"] for event in events], ["RUN_STARTED", "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END", "TOOL_CALL_RESULT", "CUSTOM", "RUN_FINISHED"])
        self.assertEqual(events[1], {"type": "TOOL_CALL_START", "toolCallId": "call-1", "toolCallName": "search", "parentMessageId": "msg-1", "rawEvent": {"name": "lead_agent"}})
        self.assertEqual(events[2], {"type": "TOOL_CALL_ARGS", "toolCallId": "call-1", "delta": '{"q": "x"}', "rawEvent": {"name": "lead_agent"}})
        self.assertEqual(events[3], {"type": "TOOL_CALL_END", "toolCallId": "call-1", "rawEvent": {"name": "lead_agent"}})
        self.assertEqual(events[4], {"type": "TOOL_CALL_RESULT", "messageId": "tool-msg-1", "toolCallId": "call-1", "content": "result", "role": "tool", "rawEvent": {"name": "lead_agent"}})
        self.assertEqual(events[5], {"type": "CUSTOM", "name": "deerflow.usage", "value": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, "rawEvent": {"name": "lead_agent"}})

    async def test_chat_agui_maps_subagent_tool_calls_and_results(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type=_stream_event_type("tool_call_chunk"), data={"task_id": "task-1", "tool_call": {"name": "websearch", "args": {"query": "deerflow"}, "id": "call-1"}}),
                StreamEvent(type=_stream_event_type("tool_result_chunk"), data={"task_id": "task-1", "tool_call_id": "call-1", "name": "websearch", "content": "result"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual(
            [event["type"] for event in events],
            ["RUN_STARTED", "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END", "TOOL_CALL_RESULT", "CUSTOM", "RUN_FINISHED"],
        )
        self.assertEqual(
            events[1],
            {
                "type": "TOOL_CALL_START",
                "toolCallId": "subagent:task-1:call-1",
                "toolCallName": "websearch",
                "parentMessageId": "subagent:task-1",
                "rawEvent": {"name": "lead_agent"},
            },
        )
        self.assertEqual(events[2], {"type": "TOOL_CALL_ARGS", "toolCallId": "subagent:task-1:call-1", "delta": '{"query": "deerflow"}', "rawEvent": {"name": "lead_agent"}})
        self.assertEqual(events[3], {"type": "TOOL_CALL_END", "toolCallId": "subagent:task-1:call-1", "rawEvent": {"name": "lead_agent"}})
        self.assertEqual(
            events[4],
            {
                "type": "TOOL_CALL_RESULT",
                "messageId": "subagent:task-1:tool-result:subagent:task-1:call-1",
                "toolCallId": "subagent:task-1:call-1",
                "content": "result",
                "role": "tool",
                "rawEvent": {"name": "lead_agent"},
            },
        )

    async def test_chat_agui_updates_subagent_tool_args_until_result(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type=_stream_event_type("tool_call_chunk"), data={"task_id": "task-1", "tool_call": {"name": "bash", "args": {"command": "echo"}, "id": "call-1"}}),
                StreamEvent(type=_stream_event_type("tool_call_chunk"), data={"task_id": "task-1", "tool_call": {"name": "bash", "args": {"command": "echo hi"}, "id": "call-1"}}),
                StreamEvent(type=_stream_event_type("tool_result_chunk"), data={"task_id": "task-1", "tool_call_id": "call-1", "name": "bash", "content": "hi"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual([event["type"] for event in events], ["RUN_STARTED", "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END", "TOOL_CALL_RESULT", "CUSTOM", "RUN_FINISHED"])
        self.assertEqual(events[2], {"type": "TOOL_CALL_ARGS", "toolCallId": "subagent:task-1:call-1", "delta": '{"command": "echo hi"}', "rawEvent": {"name": "lead_agent"}})

    async def test_chat_agui_namespaces_same_subagent_tool_id_by_task(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type=_stream_event_type("tool_call_chunk"), data={"task_id": "task-1", "tool_call": {"name": "read_file", "args": {"path": "a"}, "id": "call-1"}}),
                StreamEvent(type=_stream_event_type("tool_call_chunk"), data={"task_id": "task-2", "tool_call": {"name": "read_file", "args": {"path": "b"}, "id": "call-1"}}),
                StreamEvent(type=_stream_event_type("tool_result_chunk"), data={"task_id": "task-1", "tool_call_id": "call-1", "name": "read_file", "content": "a"}),
                StreamEvent(type=_stream_event_type("tool_result_chunk"), data={"task_id": "task-2", "tool_call_id": "call-1", "name": "read_file", "content": "b"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)
        tool_call_ids = [event["toolCallId"] for event in events if event["type"] == "TOOL_CALL_START"]

        self.assertEqual(tool_call_ids, ["subagent:task-1:call-1", "subagent:task-2:call-1"])

    async def test_chat_agui_closes_open_subagent_tool_call_on_stream_end(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type=_stream_event_type("tool_call_chunk"), data={"task_id": "task-1", "tool_call": {"name": "webfetch", "args": {"url": "https://example.com"}, "id": "call-1"}}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual([event["type"] for event in events], ["RUN_STARTED", "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END", "CUSTOM", "RUN_FINISHED"])
        self.assertEqual(events[2], {"type": "TOOL_CALL_ARGS", "toolCallId": "subagent:task-1:call-1", "delta": '{"url": "https://example.com"}', "rawEvent": {"name": "lead_agent"}})

    async def test_chat_agui_preserves_subagent_lifecycle_event_name(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type=_stream_event_type("task_failed"), data={"task_id": "task-1", "error": "boom"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual(events[1], {"type": "CUSTOM", "name": "deerflow.subagent.task_failed", "value": {"eventType": "task_failed", "task_id": "task-1", "error": "boom"}, "rawEvent": {"name": "lead_agent"}})

    async def test_chat_agui_maps_subagent_token_chunks_to_text_message_events(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type=_stream_event_type("subagent_started"), data={"task_id": "task-1", "name": "researcher", "trace_id": "trace-1"}),
                StreamEvent(type=_stream_event_type("token_chunk"), data={"task_id": "task-1", "content": "hello"}),
                StreamEvent(type=_stream_event_type("token_chunk"), data={"task_id": "task-1", "content": " world"}),
                StreamEvent(type=_stream_event_type("task_completed"), data={"task_id": "task-1", "result": "hello world"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual(
            [event for event in events if event["type"] in {"TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"}],
            [
                {"type": "TEXT_MESSAGE_START", "messageId": "subagent:task-1:message", "role": "assistant", "rawEvent": {"name": "researcher"}},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "subagent:task-1:message", "delta": "hello", "rawEvent": {"name": "researcher"}},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "subagent:task-1:message", "delta": " world", "rawEvent": {"name": "researcher"}},
                {"type": "TEXT_MESSAGE_END", "messageId": "subagent:task-1:message", "rawEvent": {"name": "lead_agent"}},
            ],
        )
        self.assertIn({"type": "CUSTOM", "name": "deerflow.subagent.token_chunk", "value": {"eventType": "token_chunk", "task_id": "task-1", "content": "hello"}, "rawEvent": {"name": "researcher"}}, events)

    async def test_chat_agui_closes_open_subagent_text_message_on_stream_end(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type=_stream_event_type("token_chunk"), data={"task_id": "task-1", "content": "partial"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual(
            [event for event in events if event["type"] in {"TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"}],
            [
                {"type": "TEXT_MESSAGE_START", "messageId": "subagent:task-1:message", "role": "assistant", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "subagent:task-1:message", "delta": "partial", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_END", "messageId": "subagent:task-1:message", "rawEvent": {"name": "lead_agent"}},
            ],
        )

    async def test_chat_agui_closes_subagent_text_on_terminal_task_events(self) -> None:
        for terminal_type in ("task_failed", "task_cancelled", "task_timed_out"):
            with self.subTest(terminal_type=terminal_type):
                fake_client = _FakeClient(
                    [
                        StreamEvent(type=_stream_event_type("token_chunk"), data={"task_id": "task-1", "content": "partial"}),
                        StreamEvent(type=_stream_event_type(terminal_type), data={"task_id": "task-1", "error": "stop"}),
                        StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
                    ]
                )

                events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)
                subagent_ends = [event for event in events if event["type"] == "TEXT_MESSAGE_END" and event["messageId"] == "subagent:task-1:message"]

                self.assertEqual(len(subagent_ends), 1)

    async def test_chat_agui_keeps_interleaved_subagent_text_messages_separate(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type=_stream_event_type("token_chunk"), data={"task_id": "task-1", "content": "a1"}),
                StreamEvent(type=_stream_event_type("token_chunk"), data={"task_id": "task-2", "content": "b1"}),
                StreamEvent(type=_stream_event_type("token_chunk"), data={"task_id": "task-1", "content": "a2"}),
                StreamEvent(type=_stream_event_type("task_completed"), data={"task_id": "task-1", "result": "a"}),
                StreamEvent(type=_stream_event_type("task_completed"), data={"task_id": "task-2", "result": "b"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual(
            [event for event in events if event["type"] in {"TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"}],
            [
                {"type": "TEXT_MESSAGE_START", "messageId": "subagent:task-1:message", "role": "assistant", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "subagent:task-1:message", "delta": "a1", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_START", "messageId": "subagent:task-2:message", "role": "assistant", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "subagent:task-2:message", "delta": "b1", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "subagent:task-1:message", "delta": "a2", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_END", "messageId": "subagent:task-1:message", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_END", "messageId": "subagent:task-2:message", "rawEvent": {"name": "lead_agent"}},
            ],
        )

    async def test_chat_agui_keeps_subagent_text_independent_from_main_text(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "main", "id": "msg-1"}),
                StreamEvent(type=_stream_event_type("token_chunk"), data={"task_id": "task-1", "content": "sub"}),
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": " done", "id": "msg-1"}),
                StreamEvent(type=_stream_event_type("task_completed"), data={"task_id": "task-1", "result": "sub"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual(
            [event for event in events if event["type"] in {"TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"}],
            [
                {"type": "TEXT_MESSAGE_START", "messageId": "msg-1", "role": "assistant", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "msg-1", "delta": "main", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_START", "messageId": "subagent:task-1:message", "role": "assistant", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "subagent:task-1:message", "delta": "sub", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "msg-1", "delta": " done", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_END", "messageId": "subagent:task-1:message", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_END", "messageId": "msg-1", "rawEvent": {"name": "lead_agent"}},
            ],
        )

    async def test_chat_agui_maps_subagent_thinking_chunks_to_reasoning_events(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type=_stream_event_type("thinking_chunk"), data={"task_id": "task-1", "thinking": "think"}),
                StreamEvent(type=_stream_event_type("thinking_chunk"), data={"task_id": "task-1", "thinking": " more"}),
                StreamEvent(type=_stream_event_type("task_completed"), data={"task_id": "task-1", "result": "done"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual(
            [event for event in events if str(event["type"]).startswith("REASONING_MESSAGE_")],
            [
                {"type": "REASONING_MESSAGE_START", "messageId": "subagent:task-1:reasoning", "role": "reasoning", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_CONTENT", "messageId": "subagent:task-1:reasoning", "delta": "think", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_CONTENT", "messageId": "subagent:task-1:reasoning", "delta": " more", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_END", "messageId": "subagent:task-1:reasoning", "rawEvent": {"name": "lead_agent"}},
            ],
        )
        self.assertIn({"type": "CUSTOM", "name": "deerflow.subagent.thinking_chunk", "value": {"eventType": "thinking_chunk", "task_id": "task-1", "thinking": "think"}, "rawEvent": {"name": "lead_agent"}}, events)

    async def test_chat_agui_closes_subagent_reasoning_before_token_text(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type=_stream_event_type("thinking_chunk"), data={"task_id": "task-1", "thinking": "think"}),
                StreamEvent(type=_stream_event_type("token_chunk"), data={"task_id": "task-1", "content": "answer"}),
                StreamEvent(type=_stream_event_type("task_completed"), data={"task_id": "task-1", "result": "answer"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual(
            [event for event in events if event["type"] in {"REASONING_MESSAGE_START", "REASONING_MESSAGE_CONTENT", "REASONING_MESSAGE_END", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT"}],
            [
                {"type": "REASONING_MESSAGE_START", "messageId": "subagent:task-1:reasoning", "role": "reasoning", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_CONTENT", "messageId": "subagent:task-1:reasoning", "delta": "think", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_END", "messageId": "subagent:task-1:reasoning", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_START", "messageId": "subagent:task-1:message", "role": "assistant", "rawEvent": {"name": "lead_agent"}},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "subagent:task-1:message", "delta": "answer", "rawEvent": {"name": "lead_agent"}},
            ],
        )

    async def test_chat_agui_closes_subagent_reasoning_on_stream_end(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type=_stream_event_type("thinking_chunk"), data={"task_id": "task-1", "thinking": "partial"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual(
            [event for event in events if str(event["type"]).startswith("REASONING_MESSAGE_")],
            [
                {"type": "REASONING_MESSAGE_START", "messageId": "subagent:task-1:reasoning", "role": "reasoning", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_CONTENT", "messageId": "subagent:task-1:reasoning", "delta": "partial", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_END", "messageId": "subagent:task-1:reasoning", "rawEvent": {"name": "lead_agent"}},
            ],
        )

    async def test_chat_agui_closes_subagent_reasoning_before_terminal_custom_event(self) -> None:
        for terminal_type in ("task_completed", "task_failed", "task_cancelled", "task_timed_out"):
            with self.subTest(terminal_type=terminal_type):
                terminal_data = {"task_id": "task-1", "result": "done"} if terminal_type == "task_completed" else {"task_id": "task-1", "error": "stop"}
                fake_client = _FakeClient(
                    [
                        StreamEvent(type=_stream_event_type("thinking_chunk"), data={"task_id": "task-1", "thinking": "partial"}),
                        StreamEvent(type=_stream_event_type(terminal_type), data=terminal_data),
                        StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
                    ]
                )

                events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)
                reasoning_end: dict[str, object] = {"type": "REASONING_MESSAGE_END", "messageId": "subagent:task-1:reasoning", "rawEvent": {"name": "lead_agent"}}
                terminal_custom = next(event for event in events if event["type"] == "CUSTOM" and event["name"] == f"deerflow.subagent.{terminal_type}")

                self.assertEqual([event for event in events if event == reasoning_end], [reasoning_end])
                self.assertLess(events.index(reasoning_end), events.index(terminal_custom))

    async def test_chat_agui_closes_subagent_reasoning_before_run_error(self) -> None:
        fake_client = _FakeClient(
            [StreamEvent(type=_stream_event_type("thinking_chunk"), data={"task_id": "task-1", "thinking": "partial"})],
            error_after_first=True,
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual(events[-2], {"type": "REASONING_MESSAGE_END", "messageId": "subagent:task-1:reasoning", "rawEvent": {"name": "lead_agent"}})
        self.assertEqual(events[-1], {"type": "RUN_ERROR", "message": "boom", "rawEvent": {"name": "lead_agent"}})

    async def test_chat_agui_keeps_main_and_subagent_reasoning_independent(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "", "id": "msg-1", "reasoning_content": "main-think"}),
                StreamEvent(type=_stream_event_type("thinking_chunk"), data={"task_id": "task-1", "thinking": "sub-think"}),
                StreamEvent(type=_stream_event_type("token_chunk"), data={"task_id": "task-1", "content": "sub-answer"}),
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "main-answer", "id": "msg-1"}),
                StreamEvent(type=_stream_event_type("task_completed"), data={"task_id": "task-1", "result": "sub-answer"}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual(
            [event for event in events if str(event["type"]).startswith("REASONING_MESSAGE_")],
            [
                {"type": "REASONING_MESSAGE_START", "messageId": "reasoning_msg-1", "role": "reasoning", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_CONTENT", "messageId": "reasoning_msg-1", "delta": "main-think", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_START", "messageId": "subagent:task-1:reasoning", "role": "reasoning", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_CONTENT", "messageId": "subagent:task-1:reasoning", "delta": "sub-think", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_END", "messageId": "subagent:task-1:reasoning", "rawEvent": {"name": "lead_agent"}},
                {"type": "REASONING_MESSAGE_END", "messageId": "reasoning_msg-1", "rawEvent": {"name": "lead_agent"}},
            ],
        )

    async def test_chat_agui_emits_run_error_on_failure(self) -> None:
        fake_client = _FakeClient(error_after_first=True)

        events, fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual(events[-2], {"type": "TEXT_MESSAGE_END", "messageId": "msg-1", "rawEvent": {"name": "lead_agent"}})
        self.assertEqual(events[-1], {"type": "RUN_ERROR", "message": "boom", "rawEvent": {"name": "lead_agent"}})
        self.assertEqual(fake_manager.done, ["thread-1"])

    async def test_chat_agui_preserves_parent_run_id_and_data_only_sse(self) -> None:
        fake_client = _FakeClient()

        events, _fake_manager, chunks = await self._collect_agui_stream(fake_client, parent_run_id="parent-1")

        self.assertEqual(events[0], {"type": "RUN_STARTED", "threadId": "thread-1", "runId": "run-1", "parentRunId": "parent-1", "rawEvent": {"name": "lead_agent"}})
        self.assertTrue(all(chunk.startswith("data: ") for chunk in chunks))
        self.assertTrue(all("event:" not in chunk for chunk in chunks))

    async def test_chat_agui_maps_values_snapshot_roles_and_tool_calls(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(
                    type="values",
                    data={
                        "title": "Chat",
                        "artifacts": [],
                        "messages": [
                            {
                                "type": "ai",
                                "id": "msg-1",
                                "content": "hello",
                                "tool_calls": [{"name": "search", "args": {"q": "x"}, "id": "call-1"}],
                            }
                        ],
                    },
                ),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        snapshot = events[1]
        self.assertEqual(snapshot["type"], "MESSAGES_SNAPSHOT")
        messages = cast(list[dict[str, object]], snapshot["messages"])
        self.assertEqual(messages[0]["role"], "assistant")
        tool_calls = cast(list[dict[str, object]], messages[0]["toolCalls"])
        self.assertEqual(tool_calls[0]["id"], "call-1")
        self.assertEqual(tool_calls[0]["type"], "function")

    async def test_chat_agui_closes_text_before_tool_call_events(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "before tool", "id": "msg-1"}),
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "", "id": "msg-1", "tool_calls": [{"name": "search", "args": {"q": "x"}, "id": "call-1"}]}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        events, _fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual(
            [event["type"] for event in events],
            ["RUN_STARTED", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END", "CUSTOM", "RUN_FINISHED"],
        )

    async def test_client_manager_async_client_uses_async_checkpointer(self) -> None:
        manager = ClientManager()
        async_checkpointer = InMemorySaver()
        captured_kwargs: list[dict[str, object]] = []

        class FakeDeerFlowClient:
            def __init__(self, **kwargs: object) -> None:
                captured_kwargs.append(kwargs)

        async def fake_get_async_checkpointer() -> InMemorySaver:
            return async_checkpointer

        manager._get_async_checkpointer = fake_get_async_checkpointer

        with patch("deerflow.client.DeerFlowClient", FakeDeerFlowClient):
            client = await manager.get_async_client()

        self.assertIsInstance(client, FakeDeerFlowClient)
        self.assertEqual(captured_kwargs[0]["checkpointer"], async_checkpointer)


if __name__ == "__main__":
    unittest.main()
