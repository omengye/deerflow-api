from collections.abc import AsyncIterator, Iterable
import asyncio
from typing import Any, override
import unittest

from app import dependencies
from app.channels.feishu import FeishuChannel, _IncomingResource, _incoming_resources
from deerflow.client import StreamEvent


class _FakeClient:
    def __init__(self, events: Iterable[StreamEvent]) -> None:
        self.events: list[StreamEvent] = list(events)
        self.last_message: str | None = None
        self.last_thread_id: str | None = None

    async def astream(self, message: str, *, thread_id: str) -> AsyncIterator[StreamEvent]:
        self.last_message = message
        self.last_thread_id = thread_id
        for event in self.events:
            yield event


class _FakeManager:
    def __init__(self, client: _FakeClient) -> None:
        self.client: _FakeClient = client

    async def get_async_client(self) -> _FakeClient:
        return self.client


class _RecordingFeishuChannel(FeishuChannel):
    def __init__(self) -> None:
        super().__init__(app_id="app", app_secret="secret")
        self.sent_cards: list[tuple[str, str, str]] = []
        self.patched_cards: list[tuple[str, str]] = []
        self.reactions: list[tuple[str, str]] = []
        self.sent_artifacts: list[tuple[str, str, tuple[str, ...]]] = []
        self.saved_resources: list[tuple[str, tuple[str, ...]]] = []
        self.incoming_uploads: list[dict[str, Any]] = []
        self.operation_log: list[str] = []
        self._next_card_number: int = 1
        self.release_send_card: asyncio.Event | None = None
        self.release_patch_card: asyncio.Event | None = None
        self.stream_started: asyncio.Event | None = None

    async def handle_message_for_test(
        self,
        client: _FakeClient,
        *,
        text: str = "prompt",
        resources: list[_IncomingResource] | None = None,
    ) -> None:
        original_get_client_manager = dependencies.get_client_manager
        dependencies.get_client_manager = lambda: _FakeManager(client)
        try:
            await self._handle_message(
                message_id="user-message",
                chat_id="chat-1",
                text=text,
                thread_id="thread-1",
                resources=resources,
            )
        finally:
            dependencies.get_client_manager = original_get_client_manager

    @override
    async def _send_card(self, chat_id: str, content: str) -> str:
        if self.release_send_card is not None:
            _ = await self.release_send_card.wait()
        card_id = f"card-{self._next_card_number}"
        self._next_card_number += 1
        self.sent_cards.append((chat_id, content, card_id))
        self.operation_log.append(f"send:{card_id}:{content}")
        return card_id

    @override
    async def _patch_card(self, card_id: str, content: str) -> None:
        if self.release_patch_card is not None:
            _ = await self.release_patch_card.wait()
        self.patched_cards.append((card_id, content))
        _ = self.operation_log.append(f"patch:{card_id}:{content}")

    @override
    async def _add_reaction(self, message_id: str, emoji_type: str) -> None:
        self.reactions.append((message_id, emoji_type))
        _ = self.operation_log.append(f"reaction:{emoji_type}")

    @override
    async def _send_artifacts(self, chat_id: str, thread_id: str, artifacts: list[str]) -> int:
        self.sent_artifacts.append((chat_id, thread_id, tuple(artifacts)))
        return len(artifacts)

    @override
    async def _save_incoming_resources(self, thread_id: str, resources: list[_IncomingResource]) -> list[dict[str, Any]]:
        self.saved_resources.append((thread_id, tuple(resource.resource_key for resource in resources)))
        return self.incoming_uploads

    @override
    async def _stream_to_cards(self, text: str, thread_id: str, chat_id: str, initial_card_id: str) -> str:
        if self.stream_started is not None:
            _ = self.stream_started.set()
        return await super()._stream_to_cards(text, thread_id, chat_id, initial_card_id)

    async def stream_to_cards(self, client: _FakeClient) -> str:
        original_get_client_manager = dependencies.get_client_manager
        dependencies.get_client_manager = lambda: _FakeManager(client)
        try:
            return await self._stream_to_cards("prompt", "thread-1", "chat-1", "initial-card")
        finally:
            dependencies.get_client_manager = original_get_client_manager


class FeishuChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_updates_same_card_for_same_ai_message_id(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "hel", "id": "ai-1", "is_delta": True}),
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "lo", "id": "ai-1", "is_delta": True}),
                StreamEvent(type="end", data={}),
            ]
        )
        channel = _RecordingFeishuChannel()
        final_text = await channel.stream_to_cards(fake_client)

        self.assertEqual(final_text, "hello")
        self.assertEqual(channel.sent_cards, [])
        self.assertEqual(channel.patched_cards[-1], ("initial-card", "hello"))
        self.assertTrue(all(card_id == "initial-card" for card_id, _content in channel.patched_cards))

    async def test_stream_creates_new_card_for_new_ai_message_id(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "first", "id": "ai-1", "is_delta": True}),
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "second", "id": "ai-2", "is_delta": True}),
                StreamEvent(type="end", data={}),
            ]
        )
        channel = _RecordingFeishuChannel()
        final_text = await channel.stream_to_cards(fake_client)

        self.assertEqual(final_text, "second")
        self.assertEqual(channel.sent_cards, [("chat-1", "second", "card-1")])
        self.assertIn(("initial-card", "first"), channel.patched_cards)
        self.assertIn(("card-1", "second"), channel.patched_cards)

    async def test_stream_ignores_non_delta_replayed_ai_text_for_card_creation(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "old", "id": "old-ai"}),
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "new", "id": "new-ai", "is_delta": True}),
                StreamEvent(type="end", data={}),
            ]
        )
        channel = _RecordingFeishuChannel()
        final_text = await channel.stream_to_cards(fake_client)

        self.assertEqual(final_text, "new")
        self.assertEqual(channel.sent_cards, [])
        self.assertEqual(channel.patched_cards[-1], ("initial-card", "new"))

    async def test_stream_waits_for_slow_partial_patch_before_final_patch(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "slow", "id": "ai-1", "is_delta": True}),
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": " final", "id": "ai-1", "is_delta": True}),
                StreamEvent(type="end", data={}),
            ]
        )
        channel = _RecordingFeishuChannel()
        channel.release_patch_card = asyncio.Event()
        stream_task = asyncio.create_task(channel.stream_to_cards(fake_client))
        await asyncio.sleep(0)

        self.assertFalse(stream_task.done())
        self.assertEqual(channel.patched_cards, [])
        _ = channel.release_patch_card.set()

        final_text = await stream_task

        self.assertEqual(final_text, "slow final")
        self.assertEqual(channel.patched_cards[-2:], [("initial-card", "slow"), ("initial-card", "slow final")])

    async def test_stream_waits_for_slow_new_card_creation_before_final_patch(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "first", "id": "ai-1", "is_delta": True}),
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "second", "id": "ai-2", "is_delta": True}),
                StreamEvent(type="end", data={}),
            ]
        )
        channel = _RecordingFeishuChannel()
        channel.release_send_card = asyncio.Event()
        stream_task = asyncio.create_task(channel.stream_to_cards(fake_client))
        await asyncio.sleep(0)

        self.assertFalse(stream_task.done())
        self.assertEqual(channel.sent_cards, [])
        _ = channel.release_send_card.set()

        final_text = await stream_task

        self.assertEqual(final_text, "second")
        self.assertEqual(channel.sent_cards, [("chat-1", "second", "card-1")])
        self.assertIn(("card-1", "second"), channel.patched_cards)

    async def test_stream_leaves_initial_card_unchanged_when_only_non_delta_ai_text_arrives(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "snapshot", "id": "old-ai"}),
                StreamEvent(type="end", data={}),
            ]
        )
        channel = _RecordingFeishuChannel()
        final_text = await channel.stream_to_cards(fake_client)

        self.assertEqual(final_text, "")
        self.assertEqual(channel.sent_cards, [])
        self.assertEqual(channel.patched_cards, [])

    async def test_stream_sends_unique_artifacts_from_values_events(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="values", data={"artifacts": ["/mnt/user-data/outputs/image.png"]}),
                StreamEvent(type="values", data={"artifacts": ["/mnt/user-data/outputs/image.png", "/mnt/user-data/outputs/report.pdf"]}),
                StreamEvent(type="end", data={}),
            ]
        )
        channel = _RecordingFeishuChannel()
        final_text = await channel.stream_to_cards(fake_client)

        self.assertEqual(final_text, "")
        self.assertEqual(
            channel.sent_artifacts,
            [("chat-1", "thread-1", ("/mnt/user-data/outputs/image.png", "/mnt/user-data/outputs/report.pdf"))],
        )
        self.assertEqual(channel.patched_cards, [("initial-card", "✅ 已发送 2 个生成文件。")])

    async def test_incoming_resources_parse_image_and_file_content(self) -> None:
        image_resources = _incoming_resources("image", "message-1", {"image_key": "img-key"})
        file_resources = _incoming_resources("file", "message-2", {"file_key": "file-key", "file_name": "report.pdf"})

        self.assertEqual(
            image_resources,
            [_IncomingResource(resource_type="image", resource_key="img-key", filename="img-key.png", message_id="message-1")],
        )
        self.assertEqual(
            file_resources,
            [_IncomingResource(resource_type="file", resource_key="file-key", filename="report.pdf", message_id="message-2")],
        )

    async def test_handle_message_passes_downloaded_resource_paths_to_agent(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "done", "id": "ai-1", "is_delta": True}),
                StreamEvent(type="end", data={}),
            ]
        )
        channel = _RecordingFeishuChannel()
        channel.incoming_uploads = [
            {
                "filename": "photo.png",
                "virtual_path": "/mnt/user-data/uploads/photo.png",
            }
        ]
        resources = [
            _IncomingResource(
                resource_type="image",
                resource_key="img-key",
                filename="photo.png",
                message_id="user-message",
            )
        ]

        await channel.handle_message_for_test(fake_client, text="分析这张图", resources=resources)

        self.assertEqual(channel.saved_resources, [("thread-1", ("img-key",))])
        self.assertEqual(fake_client.last_thread_id, "thread-1")
        self.assertIsNotNone(fake_client.last_message)
        self.assertIn("分析这张图", fake_client.last_message or "")
        self.assertIn("/mnt/user-data/uploads/photo.png", fake_client.last_message or "")
        self.assertIn("view_image", fake_client.last_message or "")

    async def test_handle_message_adds_done_reaction_after_stream_finishes(self) -> None:
        fake_client = _FakeClient(
            [
                StreamEvent(type="messages-tuple", data={"type": "ai", "content": "answer", "id": "ai-1", "is_delta": True}),
                StreamEvent(type="end", data={}),
            ]
        )
        channel = _RecordingFeishuChannel()
        channel.stream_started = asyncio.Event()
        channel.release_patch_card = asyncio.Event()
        handle_task = asyncio.create_task(channel.handle_message_for_test(fake_client))
        try:
            _ = await channel.stream_started.wait()
            await asyncio.sleep(0)

            self.assertIn(("user-message", "OK"), channel.reactions)
            self.assertNotIn(("user-message", "DONE"), channel.reactions)
            _ = channel.release_patch_card.set()
            await handle_task
        finally:
            if not handle_task.done():
                _ = handle_task.cancel()

        self.assertEqual(channel.reactions, [("user-message", "OK"), ("user-message", "DONE")])
        self.assertLess(channel.operation_log.index("patch:card-1:answer"), channel.operation_log.index("reaction:DONE"))


if __name__ == "__main__":
    _ = unittest.main()
