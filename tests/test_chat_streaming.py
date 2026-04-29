import json
import unittest
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import cast
from unittest.mock import patch

from app.dependencies import ClientManager
from app.routers import chat
from app.schemas import AguiRunAgentInput, ChatRequest
from deerflow.client import DeerFlowClient, StreamEvent
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.messages import AIMessageChunk
from langchain_core.runnables import RunnableConfig


class _FakeClient:
    def __init__(self, events: Iterable[StreamEvent] | None = None, error_after_first: bool = False) -> None:
        self.astream_called: bool = False
        self.stream_called: bool = False
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

    async def astream(self, *_args: object, **_kwargs: object) -> AsyncIterator[StreamEvent]:
        self.astream_called = True
        for index, event in enumerate(self.events):
            yield event
            if self.error_after_first and index == 0:
                raise RuntimeError("boom")


class _FakeManager:
    def __init__(self, client: _FakeClient) -> None:
        self.client: _FakeClient = client
        self.running: list[str] = []
        self.done: list[str] = []

    def get_client(self) -> _FakeClient:
        return self.client

    async def get_async_client(self) -> _FakeClient:
        return self.client

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
            response = await chat.chat_agui(self._agui_request(parent_run_id=parent_run_id))
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

    async def _collect_chat_stream(self, fake_client: _FakeClient) -> tuple[list[dict[str, str]], _FakeManager]:
        fake_manager = _FakeManager(fake_client)
        original_get_client_manager = chat.get_client_manager
        chat.get_client_manager = lambda: fake_manager
        try:
            response = await chat.chat_stream(ChatRequest(message="hello", thread_id="thread-1"))
            chunks = [cast(dict[str, str], chunk) async for chunk in response.body_iterator]
        finally:
            chat.get_client_manager = original_get_client_manager
        return chunks, fake_manager

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
        self.assertEqual(events[0], StreamEvent(type="messages-tuple", data={"type": "ai", "content": "hello", "id": "msg-1"}))
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

        first_event = chunks[0]
        second_event = chunks[1]
        third_event = chunks[2]
        self.assertEqual(first_event["event"], "messages-tuple")
        self.assertEqual(json.loads(first_event["data"]), {"type": "ai", "content": "hello", "id": "msg-1"})
        self.assertEqual(second_event["event"], "text")
        self.assertEqual(json.loads(second_event["data"]), {"content": "hello", "thread_id": "thread-1"})
        self.assertEqual(third_event["event"], "end")

    async def test_chat_stream_does_not_emit_text_event_for_tool_calls(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "", "id": "msg-1", "tool_calls": [{"name": "tool", "args": {}, "id": "call-1"}]}),
                StreamEvent(type="end", data={"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
            ]
        )

        chunks, _fake_manager = await self._collect_chat_stream(fake_client)

        self.assertEqual([chunk["event"] for chunk in chunks], ["messages-tuple", "end"])

    async def test_chat_stream_emits_error_and_cleans_up_after_midstream_failure(self) -> None:
        fake_client = _FakeClient(error_after_first=True)

        chunks, fake_manager = await self._collect_chat_stream(fake_client)

        self.assertEqual([chunk["event"] for chunk in chunks], ["messages-tuple", "text", "error"])
        self.assertEqual(json.loads(chunks[-1]["data"]), {"error": "boom"})
        self.assertEqual(fake_manager.done, ["thread-1"])

    async def test_chat_agui_emits_protocol_lifecycle_and_text_events(self) -> None:
        fake_client = _FakeClient()

        events, fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual([event["type"] for event in events], ["RUN_STARTED", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "CUSTOM", "RUN_FINISHED"])
        self.assertEqual(events[0], {"type": "RUN_STARTED", "threadId": "thread-1", "runId": "run-1"})
        self.assertEqual(events[1], {"type": "TEXT_MESSAGE_START", "messageId": "msg-1", "role": "assistant"})
        self.assertEqual(events[2], {"type": "TEXT_MESSAGE_CONTENT", "messageId": "msg-1", "delta": "hello"})
        self.assertEqual(events[3], {"type": "TEXT_MESSAGE_END", "messageId": "msg-1"})
        self.assertEqual(events[-1], {"type": "RUN_FINISHED", "threadId": "thread-1", "runId": "run-1"})
        self.assertEqual(fake_manager.done, ["thread-1"])

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
        self.assertEqual(events[1], {"type": "TOOL_CALL_START", "toolCallId": "call-1", "toolCallName": "search", "parentMessageId": "msg-1"})
        self.assertEqual(events[2], {"type": "TOOL_CALL_ARGS", "toolCallId": "call-1", "delta": '{"q": "x"}'})
        self.assertEqual(events[3], {"type": "TOOL_CALL_END", "toolCallId": "call-1"})
        self.assertEqual(events[4], {"type": "TOOL_CALL_RESULT", "messageId": "tool-msg-1", "toolCallId": "call-1", "content": "result", "role": "tool"})

    async def test_chat_agui_emits_run_error_on_failure(self) -> None:
        fake_client = _FakeClient(error_after_first=True)

        events, fake_manager, _chunks = await self._collect_agui_stream(fake_client)

        self.assertEqual(events[-2], {"type": "TEXT_MESSAGE_END", "messageId": "msg-1"})
        self.assertEqual(events[-1], {"type": "RUN_ERROR", "message": "boom"})
        self.assertEqual(fake_manager.done, ["thread-1"])

    async def test_chat_agui_preserves_parent_run_id_and_data_only_sse(self) -> None:
        fake_client = _FakeClient()

        events, _fake_manager, chunks = await self._collect_agui_stream(fake_client, parent_run_id="parent-1")

        self.assertEqual(events[0], {"type": "RUN_STARTED", "threadId": "thread-1", "runId": "run-1", "parentRunId": "parent-1"})
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
