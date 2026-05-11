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
from dataclasses import dataclass
import json
import logging
import mimetypes
from pathlib import Path
import tempfile
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_PATCH_INTERVAL = 1.0  # seconds between card patch calls
_RESET_COMMANDS = frozenset({"/new", "/reset"})
_MAX_FEISHU_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_FEISHU_FILE_BYTES = 30 * 1024 * 1024
_FEISHU_IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".ico", ".tiff", ".heic"}
)
_VIEW_IMAGE_SUPPORTED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


@dataclass(frozen=True)
class _IncomingResource:
    resource_type: str
    resource_key: str
    filename: str
    message_id: str


def _feishu_file_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".doc", ".docx"}:
        return "doc"
    if suffix in {".xls", ".xlsx"}:
        return "xls"
    if suffix in {".ppt", ".pptx"}:
        return "ppt"
    if suffix == ".mp4":
        return "mp4"
    if suffix == ".opus":
        return "opus"
    return "stream"


def _is_feishu_image(path: Path) -> bool:
    if path.suffix.lower() in _FEISHU_IMAGE_EXTENSIONS:
        return True
    mime_type, _ = mimetypes.guess_type(path)
    return bool(mime_type and mime_type.startswith("image/"))


def _detect_supported_image_extension(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _filename_for_incoming_resource(resource: _IncomingResource, response_filename: str, data: bytes) -> str:
    filename = response_filename or resource.filename
    if resource.resource_type != "image":
        return filename

    detected_suffix = _detect_supported_image_extension(data)
    if detected_suffix is None:
        return filename

    suffix = Path(filename).suffix.lower()
    if suffix == detected_suffix or (detected_suffix == ".jpg" and suffix == ".jpeg"):
        return filename

    if suffix in _VIEW_IMAGE_SUPPORTED_EXTENSIONS:
        return f"{Path(filename).stem}{detected_suffix}"
    if not suffix:
        return f"{filename}{detected_suffix}"
    return filename


def _content_dict(raw_content: str | None) -> dict[str, Any]:
    try:
        content = json.loads(raw_content or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return content if isinstance(content, dict) else {}


def _incoming_resources(message_type: str, message_id: str, content: dict[str, Any]) -> list[_IncomingResource]:
    if message_type == "image":
        image_key = content.get("image_key") or content.get("file_key")
        if not isinstance(image_key, str) or not image_key:
            return []
        filename = content.get("file_name")
        if not isinstance(filename, str) or not filename:
            filename = f"{image_key}.png"
        return [
            _IncomingResource(
                resource_type="image",
                resource_key=image_key,
                filename=filename,
                message_id=message_id,
            )
        ]

    if message_type == "file":
        file_key = content.get("file_key")
        if not isinstance(file_key, str) or not file_key:
            return []
        filename = content.get("file_name")
        if not isinstance(filename, str) or not filename:
            filename = file_key
        return [
            _IncomingResource(
                resource_type="file",
                resource_key=file_key,
                filename=filename,
                message_id=message_id,
            )
        ]

    return []


def _build_prompt_with_uploads(text: str, uploaded_files: list[dict[str, Any]]) -> str:
    if not uploaded_files:
        return text

    lines = [text] if text else ["请处理用户通过飞书上传的文件。"]
    lines.append("用户通过飞书上传的文件已保存到本轮会话的 /mnt/user-data/uploads：")
    for uploaded in uploaded_files:
        filename = uploaded.get("filename")
        virtual_path = uploaded.get("virtual_path")
        if isinstance(filename, str) and isinstance(virtual_path, str):
            line = f"- {filename}: {virtual_path}"
        elif isinstance(virtual_path, str):
            line = f"- {virtual_path}"
        else:
            continue

        markdown_virtual_path = uploaded.get("markdown_virtual_path")
        if isinstance(markdown_virtual_path, str):
            line = f"{line}（已转换 Markdown: {markdown_virtual_path}）"
        lines.append(line)
    lines.append("如需查看图片，请使用 view_image；如需读取文档，请优先使用 read_file、grep 或 glob。")
    lines.append("如需返回生成文件，请保存到 /mnt/user-data/outputs 并调用 present_files。")
    return "\n\n".join(lines)


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
        # Feishu receives full-state snapshots on every turn. Track artifacts
        # already sent per thread so historical generated files are not resent.
        self._sent_artifacts_by_thread: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, main_loop: asyncio.AbstractEventLoop) -> None:
        """Build the Feishu client, fetch bot info, and start the WS thread."""
        import lark_oapi as lark

        self._main_loop = main_loop
        self._lark_client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .build()
        )
        await self._fetch_bot_info()
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

    async def _fetch_bot_info(self) -> None:
        # lark-oapi 1.6.x removed `lark_oapi.api.bot.v3`; the SDK now exposes
        # bot identity via the channel helper, which calls /bot/v3/info on the
        # raw transport (and falls back to /application/v6).
        try:
            from lark_oapi.channel.bot_identity import fetch_bot_identity

            identity = await fetch_bot_identity(self._lark_client.config)
            if identity and identity.open_id:
                self._bot_open_id = identity.open_id
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

            message_type: str = msg.message_type
            if message_type not in {"text", "image", "file"}:
                return
            if getattr(sender, "sender_type", None) == "app":
                return

            chat_id: str = msg.chat_id
            chat_type: str = msg.chat_type
            message_id: str = msg.message_id

            content = _content_dict(msg.content)
            raw_text = content.get("text", "") if message_type == "text" else ""
            if not isinstance(raw_text, str):
                raw_text = ""
            resources = _incoming_resources(message_type, message_id, content)

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

            if not text and not resources:
                return

            if self._main_loop is None:
                logger.error("Feishu channel received a message before start() initialised the main loop")
                return

            fut = asyncio.run_coroutine_threadsafe(
                self._handle_message(
                    message_id=message_id,
                    chat_id=chat_id,
                    text=text,
                    resources=resources,
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
        resources: list[_IncomingResource] | None = None,
    ) -> None:
        """Full pipeline for one incoming message; serialised per chat_id."""
        resources = resources or []
        if not resources and text.lower() in _RESET_COMMANDS:
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
                prompt = text
                if resources:
                    try:
                        uploaded_files = await self._save_incoming_resources(thread_id, resources)
                    except Exception as exc:
                        logger.exception("Failed to save Feishu resources (thread=%s)", thread_id)
                        await self._patch_card(
                            card_id,
                            f"⚠️ 文件下载或保存失败，请稍后重试。\n{type(exc).__name__}: {exc}",
                        )
                        return
                    if not uploaded_files:
                        logger.warning("Feishu resources produced no uploaded files (thread=%s)", thread_id)
                        await self._patch_card(card_id, "⚠️ 没有成功保存飞书上传的文件，请稍后重试。")
                        return
                    prompt = _build_prompt_with_uploads(text, uploaded_files)
                _ = await self._stream_to_cards(prompt, thread_id, chat_id, card_id)
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
            self._sent_artifacts_by_thread.pop(thread_id, None)
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
        artifacts: list[str] = []
        seen_artifacts: set[str] = set()
        sent_artifact_paths = self._sent_artifacts_by_thread.setdefault(thread_id, set())
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

        def collect_artifacts(data: Any) -> None:
            if not isinstance(data, dict):
                return
            raw_artifacts = data.get("artifacts")
            if not isinstance(raw_artifacts, list):
                return
            for artifact in raw_artifacts:
                if (
                    isinstance(artifact, str)
                    and artifact not in seen_artifacts
                    and artifact not in sent_artifact_paths
                ):
                    seen_artifacts.add(artifact)
                    artifacts.append(artifact)

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
            elif event.type == "values":
                collect_artifacts(event.data)

        # Ensure in-flight patches finish before final full-content patches.  This
        # prevents an older partial patch from racing after a final patch.
        for msg_id in list(patch_tasks):
            await drain_patch_task(msg_id)

        for msg_id, parts in chunks.items():
            final_text = "".join(parts)
            if final_text:
                await patch_message_card(msg_id, final_text)

        sent_artifact_count = await self._send_artifacts(chat_id, thread_id, artifacts)
        if sent_artifact_count and not chunks and initial_card_id:
            await self._patch_card(initial_card_id, f"✅ 已发送 {sent_artifact_count} 个生成文件。")

        return "".join(chunks.get(last_id, ()))

    # ------------------------------------------------------------------
    # Internal: Feishu API helpers (sync SDK wrapped in asyncio.to_thread)
    # ------------------------------------------------------------------

    async def _save_incoming_resources(self, thread_id: str, resources: list[_IncomingResource]) -> list[dict[str, Any]]:
        """Download Feishu message resources and register them as thread uploads."""
        if not resources:
            return []

        from app.dependencies import get_client_manager
        from deerflow.uploads.manager import claim_unique_filename, normalize_filename

        with tempfile.TemporaryDirectory(prefix="feishu-incoming-") as tmpdir:
            tmp_paths: list[Path] = []
            seen_names: set[str] = set()
            for resource in resources:
                data, response_filename = await self._download_message_resource(resource)
                filename = normalize_filename(_filename_for_incoming_resource(resource, response_filename, data))
                tmp_name = claim_unique_filename(filename, seen_names)
                tmp_path = Path(tmpdir) / tmp_name
                tmp_path.write_bytes(data)
                tmp_paths.append(tmp_path)

            client = get_client_manager().get_client()
            result = await asyncio.to_thread(client.upload_files, thread_id, tmp_paths)
        files = result.get("files", []) if isinstance(result, dict) else []
        return [item for item in files if isinstance(item, dict)]

    async def _download_message_resource(self, resource: _IncomingResource) -> tuple[bytes, str]:
        """Download one image/file resource from a Feishu message."""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        req = (
            GetMessageResourceRequest.builder()
            .message_id(resource.message_id)
            .file_key(resource.resource_key)
            .type(resource.resource_type)
            .build()
        )
        resp = await asyncio.to_thread(self._lark_client.im.v1.message_resource.get, req)
        if not resp.success():
            raise RuntimeError(
                f"Feishu resource download failed: code={resp.code} msg={resp.msg}"
            )
        file_obj = getattr(resp, "file", None)
        if file_obj is None:
            raise RuntimeError("Feishu resource download did not return a file stream")
        data = await asyncio.to_thread(file_obj.read)
        if not isinstance(data, bytes):
            raise RuntimeError("Feishu resource download did not return bytes")
        filename = getattr(resp, "file_name", "") or ""
        return data, filename

    async def _send_artifacts(self, chat_id: str, thread_id: str, artifacts: list[str]) -> int:
        """Upload agent-presented artifacts to Feishu and send them to chat_id."""
        if not artifacts:
            return 0

        from deerflow.config.paths import get_paths

        sent = 0
        paths = get_paths()
        for artifact in artifacts:
            try:
                artifact_path = paths.resolve_virtual_path(thread_id, artifact)
                if not artifact_path.is_file():
                    logger.warning("Skipping non-file Feishu artifact: %s", artifact)
                    continue
                await self._send_artifact(chat_id, artifact_path)
                self._sent_artifacts_by_thread.setdefault(thread_id, set()).add(artifact)
                sent += 1
            except Exception as exc:
                logger.warning("Failed to send Feishu artifact %s", artifact, exc_info=True)
                await self._send_card(
                    chat_id,
                    f"⚠️ 生成文件发送失败：{artifact}\n{type(exc).__name__}: {exc}",
                )
        return sent

    async def _send_artifact(self, chat_id: str, artifact_path: Path) -> None:
        size = artifact_path.stat().st_size
        if _is_feishu_image(artifact_path) and size <= _MAX_FEISHU_IMAGE_BYTES:
            image_key = await self._upload_image(artifact_path)
            await self._send_resource_message(chat_id, "image", "image_key", image_key)
            return

        if size > _MAX_FEISHU_FILE_BYTES:
            raise ValueError(
                f"{artifact_path.name} is {size} bytes, exceeding Feishu file limit {_MAX_FEISHU_FILE_BYTES} bytes"
            )

        file_key = await self._upload_file(artifact_path)
        await self._send_resource_message(chat_id, "file", "file_key", file_key)

    async def _upload_image(self, path: Path) -> str:
        """Upload an image to Feishu and return its image_key."""
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

        with path.open("rb") as file:
            req = (
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(file)
                    .build()
                )
                .build()
            )
            resp = await asyncio.to_thread(self._lark_client.im.v1.image.create, req)
        if not resp.success():
            raise RuntimeError(f"Feishu image upload failed: code={resp.code} msg={resp.msg}")
        image_key = getattr(resp.data, "image_key", "") if resp.data else ""
        if not image_key:
            raise RuntimeError("Feishu image upload did not return image_key")
        return image_key

    async def _upload_file(self, path: Path) -> str:
        """Upload a file to Feishu and return its file_key."""
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        with path.open("rb") as file:
            req = (
                CreateFileRequest.builder()
                .request_body(
                    CreateFileRequestBody.builder()
                    .file_type(_feishu_file_type(path))
                    .file_name(path.name)
                    .file(file)
                    .build()
                )
                .build()
            )
            resp = await asyncio.to_thread(self._lark_client.im.v1.file.create, req)
        if not resp.success():
            raise RuntimeError(f"Feishu file upload failed: code={resp.code} msg={resp.msg}")
        file_key = getattr(resp.data, "file_key", "") if resp.data else ""
        if not file_key:
            raise RuntimeError("Feishu file upload did not return file_key")
        return file_key

    async def _send_resource_message(self, chat_id: str, msg_type: str, key_name: str, key: str) -> str:
        """Send an uploaded Feishu image/file resource to a chat."""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(json.dumps({key_name: key}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = await asyncio.to_thread(self._lark_client.im.v1.message.create, req)
        if not resp.success():
            raise RuntimeError(f"Feishu {msg_type} send failed: code={resp.code} msg={resp.msg}")
        return (resp.data.message_id or "") if resp.data else ""

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
