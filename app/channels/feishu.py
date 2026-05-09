"""Feishu (Lark) channel — WebSocket long-connection mode.

Message flow per incoming text:
  1. Add "OK" emoji reaction to acknowledge receipt
  2. Send an interactive card with "Working on it..."
  3. Stream agent response; bind each AI message id to one card
  4. Patch the matching card every _PATCH_INTERVAL seconds; add "DONE" reaction

Only @bot mentions in group chats are handled.  P2P chats always respond.
thread_id is prefixed as "feishu:{chat_id}" to isolate from HTTP API threads.
"""
import asyncio
import json
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_PATCH_INTERVAL = 1.0  # seconds between card patch calls
_RESET_COMMANDS = frozenset({"/new", "/reset"})


def _shutdown_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel all tasks and close a loop — mirrors asyncio.run() teardown.

    Without this, uncleaned tasks holding open SSL transports are left dangling.
    On Python 3.14, BaseEventLoop.__del__ calls loop.close() during GC; any
    transport callback that then calls loop.call_soon() raises
    RuntimeError('Event loop is closed'), which can surface in unrelated
    coroutines running on a different loop at that moment.
    """
    try:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        loop.close()


def _make_card(content: str) -> str:
    return json.dumps(
        {
            "config": {"wide_screen_mode": True},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}}
            ],
        },
        ensure_ascii=False,
    )


class FeishuChannel:
    def __init__(self, app_id: str, app_secret: str, verification_token: str = "") -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._verification_token = verification_token
        self._lark_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._bot_open_id: str | None = None
        # Keyed by chat_id; values are asyncio.Lock used inside _main_loop.
        self._chat_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, main_loop: asyncio.AbstractEventLoop) -> None:
        """Build the Feishu client, fetch bot info, and start the WS thread."""
        import lark_oapi as lark

        self._main_loop = main_loop
        self._lark_client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .build()
        )
        self._fetch_bot_info()
        self._ws_thread = threading.Thread(
            target=self._run_ws, daemon=True, name="feishu-ws"
        )
        self._ws_thread.start()
        logger.info(
            "Feishu channel started (app_id=%s bot_open_id=%s)",
            self._app_id,
            self._bot_open_id,
        )

    def stop(self) -> None:
        """Signal the WS event loop to stop; the daemon thread will exit."""
        loop = self._ws_loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)

    # ------------------------------------------------------------------
    # Internal: WebSocket thread
    # ------------------------------------------------------------------

    def _fetch_bot_info(self) -> None:
        try:
            from lark_oapi.api.bot.v3 import BotGetRequest

            resp = self._lark_client.bot.v3.bot.get(BotGetRequest.builder().build())
            if resp.success() and resp.data and resp.data.bot:
                self._bot_open_id = resp.data.bot.open_id
        except Exception:
            logger.warning(
                "Could not fetch Feishu bot open_id; group @-filter disabled",
                exc_info=True,
            )

    def _run_ws(self) -> None:
        import lark_oapi as lark
        import lark_oapi.ws.client as _lark_ws_client

        loop = asyncio.new_event_loop()
        self._ws_loop = loop
        asyncio.set_event_loop(loop)
        # lark_oapi.ws.client captures `loop` as a module-level global at import
        # time (which is the already-running FastAPI main loop).  We must replace
        # it with our fresh thread-local loop so Client.start() can call
        # loop.run_until_complete() without hitting "event loop already running".
        _lark_ws_client.loop = loop

        dispatcher = (
            lark.EventDispatcherHandler.builder(
                self._verification_token,
                self._app_secret,
            )
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
        # ws.Client is constructed directly — no builder pattern.
        ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=dispatcher,
        )
        try:
            ws_client.start()
        except Exception:
            logger.exception("Feishu WebSocket exited with error")
        finally:
            try:
                _shutdown_loop(loop)
            except Exception:
                logger.debug("WS loop cleanup error", exc_info=True)

    # ------------------------------------------------------------------
    # Internal: message routing (sync — called by lark-oapi WS thread)
    # ------------------------------------------------------------------

    def _on_message(self, data: Any) -> None:
        """Sync handler called by lark-oapi for each IM message event.

        Parses and validates the event, then schedules async processing
        in the FastAPI main event loop via run_coroutine_threadsafe.
        """
        try:
            msg = data.event.message
            sender = data.event.sender

            if msg.message_type != "text":
                return
            if getattr(sender, "sender_type", None) == "app":
                return

            chat_id: str = msg.chat_id
            chat_type: str = msg.chat_type
            message_id: str = msg.message_id

            try:
                raw_text: str = json.loads(msg.content or "{}").get("text", "")
            except (json.JSONDecodeError, AttributeError):
                return

            mentions: list[Any] = list(msg.mentions or [])

            # Group chats: only respond when the bot is @mentioned.
            if chat_type == "group":
                if self._bot_open_id:
                    mentioned = any(
                        getattr(getattr(m, "id", None), "open_id", None) == self._bot_open_id
                        for m in mentions
                    )
                else:
                    # bot_open_id unknown — fall back to "any mention present"
                    mentioned = bool(mentions)
                if not mentioned:
                    return

            # Strip @mention tokens from the text body.
            for m in mentions:
                key = getattr(m, "key", "")
                if key:
                    raw_text = raw_text.replace(key, "")
            text = raw_text.strip()

            if not text:
                return

            if self._main_loop is None:
                logger.error("Feishu channel received a message before start() initialised the main loop")
                return

            fut = asyncio.run_coroutine_threadsafe(
                self._handle_message(
                    message_id=message_id,
                    chat_id=chat_id,
                    text=text,
                    thread_id=f"feishu_{chat_id}",
                ),
                self._main_loop,
            )
            fut.add_done_callback(
                lambda f: logger.exception(
                    "Feishu message handler error", exc_info=f.exception()
                )
                if f.exception()
                else None
            )
        except Exception:
            logger.exception("Error in Feishu _on_message")

    # ------------------------------------------------------------------
    # Internal: message handling pipeline (runs in _main_loop)
    # ------------------------------------------------------------------

    async def _handle_message(
        self,
        *,
        message_id: str,
        chat_id: str,
        text: str,
        thread_id: str,
    ) -> None:
        """Full pipeline for one incoming message; serialised per chat_id."""
        if text.lower() in _RESET_COMMANDS:
            await self._handle_reset(message_id=message_id, chat_id=chat_id, thread_id=thread_id)
            return

        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            logger.info("Feishu chat %s busy — skipping message %s", chat_id, message_id)
            return

        async with lock:
            await self._add_reaction(message_id, "OK")
            card_id = await self._send_card(chat_id, "⏳ Working on it...")
            try:
                _ = await self._stream_to_cards(text, thread_id, chat_id, card_id)
            except Exception:
                logger.exception("Stream error (thread=%s)", thread_id)
                await self._patch_card(card_id, "❌ An error occurred. Please try again.")
            finally:
                await self._add_reaction(message_id, "DONE")

    async def _handle_reset(
        self,
        *,
        message_id: str,
        chat_id: str,
        thread_id: str,
    ) -> None:
        """Handle /new and /reset: delete thread history and confirm to user."""
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            logger.info("Feishu chat %s busy — skipping reset", chat_id)
            return
        async with lock:
            from app.dependencies import get_client_manager

            result = get_client_manager().delete_thread_completely(thread_id)
            # Remove the lock entry so the next message starts with a clean state.
            self._chat_locks.pop(chat_id, None)
            await self._add_reaction(message_id, "DONE")
            if result.get("success"):
                await self._send_card(chat_id, "✅ 会话已重置，开始新对话吧！")
            elif result.get("running"):
                await self._send_card(chat_id, "⏳ 正在处理中，请稍后再试。")
            else:
                await self._send_card(chat_id, "❌ 重置失败，请稍后再试。")

    async def _stream_to_cards(
        self,
        text: str,
        thread_id: str,
        chat_id: str,
        initial_card_id: str,
    ) -> str:
        """Consume astream(), updating one Feishu card per AI message id.

        Groups deltas by AI message id (mirrors client.chat()) so that historical
        messages replayed in LangGraph values-mode snapshots are not re-appended
        to the current response.  Each streaming AI message id owns exactly one
        Feishu card: chunks with the same id patch that card, while a new id gets
        a new card.  The initial "Working on it..." card is reused for the first
        streaming AI message so the acknowledgement card does not become stale.

        Card patches are fired as background tasks so they never suspend the LLM
        streaming loop.  Suspending astream() mid-iteration while waiting for an
        asyncio.to_thread call can leave LLM SSL transports in a partially-drained
        state; on Python 3.14 this interacts with the GC-driven WS loop teardown
        and surfaces as RuntimeError('Event loop is closed') inside the LLM call.
        """
        from app.dependencies import get_client_manager

        client = await get_client_manager().get_async_client()
        chunks: dict[str, list[str]] = {}
        card_ids: dict[str, str] = {}
        card_tasks: dict[str, asyncio.Task[str]] = {}
        patch_tasks: dict[str, asyncio.Task[None]] = {}
        last_patch_at: dict[str, float] = {}
        last_id = ""
        initial_card_bound = False

        def ensure_card_for_message(msg_id: str, initial_content: str) -> None:
            """Bind msg_id to a Feishu card, creating one in the background if needed."""
            nonlocal initial_card_bound
            if msg_id in card_ids or msg_id in card_tasks:
                return
            if initial_card_id and not initial_card_bound:
                card_ids[msg_id] = initial_card_id
                initial_card_bound = True
                return
            card_tasks[msg_id] = asyncio.create_task(
                self._send_card(chat_id, initial_content or "⏳ Working on it...")
            )

        async def resolve_card_id(msg_id: str) -> str:
            if msg_id in card_ids:
                return card_ids[msg_id]
            task = card_tasks.get(msg_id)
            if task is None:
                return ""
            try:
                card_id = await task
            except Exception:
                logger.warning("send_card task failed for AI message %s", msg_id, exc_info=True)
                return ""
            card_ids[msg_id] = card_id
            return card_id

        async def patch_message_card(msg_id: str, content: str) -> None:
            card_id = await resolve_card_id(msg_id)
            if card_id:
                await self._patch_card(card_id, content)

        async def drain_patch_task(msg_id: str) -> None:
            task = patch_tasks.get(msg_id)
            if task is None:
                return
            try:
                await task
            except Exception:
                logger.warning("patch_card task failed for AI message %s", msg_id, exc_info=True)

        def schedule_patch(msg_id: str, content: str) -> None:
            task = patch_tasks.get(msg_id)
            if task is None or task.done():
                patch_tasks[msg_id] = asyncio.create_task(patch_message_card(msg_id, content))

        async for event in client.astream(text, thread_id=thread_id):
            if event.type == "messages-tuple":
                data = event.data
                # LangGraph wraps stream items as [chunk_dict, metadata_dict].
                if isinstance(data, list) and data:
                    data = data[0]
                if isinstance(data, dict):
                    msg_type = data.get("type", "")
                    if msg_type in ("ai", "AIMessage", "AIMessageChunk"):
                        content = data.get("content", "")
                        msg_id = data.get("id") or ""
                        # Exclude tool-call chunks — they have no displayable text.
                        if (
                            msg_id
                            and data.get("is_delta")
                            and isinstance(content, str)
                            and content
                            and not data.get("tool_calls")
                        ):
                            chunks.setdefault(msg_id, []).append(content)
                            ensure_card_for_message(msg_id, content)
                            last_id = msg_id

                            current = "".join(chunks[msg_id])
                            now = time.monotonic()
                            if current and (now - last_patch_at.get(msg_id, 0.0)) >= _PATCH_INTERVAL:
                                # Fire patch as a background task — do NOT await here so the
                                # async-for loop is never suspended waiting for the Feishu API.
                                schedule_patch(msg_id, current)
                                last_patch_at[msg_id] = now

        # Ensure in-flight patches finish before final full-content patches.  This
        # prevents an older partial patch from racing after a final patch.
        for msg_id in list(patch_tasks):
            await drain_patch_task(msg_id)

        for msg_id, parts in chunks.items():
            final_text = "".join(parts)
            if final_text:
                await patch_message_card(msg_id, final_text)

        return "".join(chunks.get(last_id, ()))

    # ------------------------------------------------------------------
    # Internal: Feishu API helpers (sync SDK wrapped in asyncio.to_thread)
    # ------------------------------------------------------------------

    async def _send_card(self, chat_id: str, content: str) -> str:
        """Send an interactive card to chat_id; return the Feishu message_id."""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(_make_card(content))
                .build()
            )
            .build()
        )
        resp = await asyncio.to_thread(self._lark_client.im.v1.message.create, req)
        if not resp.success():
            logger.error("send_card failed: code=%s msg=%s", resp.code, resp.msg)
            return ""
        return (resp.data.message_id or "") if resp.data else ""

    async def _patch_card(self, card_id: str, content: str) -> None:
        """Update an existing interactive card with new content."""
        if not card_id:
            return
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        req = (
            PatchMessageRequest.builder()
            .message_id(card_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(_make_card(content))
                .build()
            )
            .build()
        )
        resp = await asyncio.to_thread(self._lark_client.im.v1.message.patch, req)
        if not resp.success():
            logger.warning("patch_card failed: code=%s msg=%s", resp.code, resp.msg)

    async def _add_reaction(self, message_id: str, emoji_type: str) -> None:
        """Add an emoji reaction to a message (fire-and-forget on failure)."""
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
        )
        from lark_oapi.api.im.v1.model.emoji import Emoji

        req = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                .build()
            )
            .build()
        )
        resp = await asyncio.to_thread(
            self._lark_client.im.v1.message_reaction.create, req
        )
        if not resp.success():
            logger.warning(
                "add_reaction(%s) failed: code=%s msg=%s", emoji_type, resp.code, resp.msg
            )
