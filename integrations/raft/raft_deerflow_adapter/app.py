"""Long-running orchestration loop for Raft and DeerFlow ACP."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from .acp_client import DeerFlowACPClient
from .activity import ActivityQueue
from .config import AdapterConfig
from .models import PendingMessage, RaftCheckResult
from .raft_cli import RaftCLI, RaftDeliveryUnknownError, RaftTransportError
from .state import AdapterState
from .wake_server import WakeServer

logger = logging.getLogger(__name__)


class AdapterApp:
    def __init__(self, config: AdapterConfig) -> None:
        self.config = config
        self.state = AdapterState(config.state_path)
        self.raft = RaftCLI(
            config.raft_command,
            config.raft_args,
            config.raft_profile,
        )
        self.activity = ActivityQueue()
        self.acp = DeerFlowACPClient(
            config.deerflow_command,
            config.deerflow_args,
            config.workspace,
            timeout_seconds=config.acp_timeout_seconds,
            activity=self.activity,
        )
        self.wake = WakeServer(
            config.wake_host,
            config.wake_port,
            config.wake_token,
            config.runtime_session,
            self._on_wake,
            activity=self.activity,
        )
        self._drain_requested = asyncio.Event()
        self._stop_requested = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._inbox_transport_failure_streak = 0

    async def start(self, *, start_bridge: bool | None = None) -> None:
        await self.acp.open()
        await self.wake.start()
        self.activity.emit(
            "SessionStart", session_id=self.config.runtime_session
        )
        logger.info("Wake endpoint listening at %s", self.wake.wake_endpoint)
        self._tasks.append(
            asyncio.create_task(self._drain_loop(), name="raft-inbox-drain")
        )
        should_start_bridge = (
            self.config.start_bridge if start_bridge is None else start_bridge
        )
        if should_start_bridge:
            self._tasks.append(
                asyncio.create_task(self._bridge_supervisor(), name="raft-wake-bridge")
            )
        self._drain_requested.set()

    async def run(self) -> None:
        await self.start()
        try:
            await self._stop_requested.wait()
        finally:
            await self.close()

    async def close(self) -> None:
        self._stop_requested.set()
        self.activity.emit("SessionEnd", session_id=self.config.runtime_session)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.wake.close()
        await self.acp.close()
        self.state.close()

    async def run_once(self) -> None:
        await self.acp.open()
        try:
            await self.drain_once()
        finally:
            await self.acp.close()
            self.state.close()

    async def _on_wake(self, payload: dict[str, object]) -> None:
        logger.info(
            "Received Raft wake event=%s message=%s",
            payload.get("eventId"),
            payload.get("messageId"),
        )
        self._drain_requested.set()

    async def _drain_loop(self) -> None:
        while not self._stop_requested.is_set():
            try:
                await asyncio.wait_for(
                    self._drain_requested.wait(),
                    timeout=self.config.poll_interval_seconds,
                )
            except TimeoutError:
                pass
            self._drain_requested.clear()
            try:
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except RaftTransportError as exc:
                self._inbox_transport_failure_streak += 1
                log = (
                    logger.error
                    if self._inbox_transport_failure_streak >= 3
                    else logger.warning
                )
                log(
                    "Raft inbox transport unavailable after %d attempt(s) "
                    "(failed drain cycles=%d); will retry on the next wake/poll: %s",
                    self.config.inbox_transport_retry_attempts,
                    self._inbox_transport_failure_streak,
                    exc,
                )
            except Exception:
                logger.exception("Raft inbox drain failed")
            else:
                if self._inbox_transport_failure_streak:
                    logger.info(
                        "Raft inbox transport recovered after %d failed drain cycle(s)",
                        self._inbox_transport_failure_streak,
                    )
                    self._inbox_transport_failure_streak = 0

    async def drain_once(self) -> None:
        await self._process_pending()
        while True:
            result = await self._check_messages_with_retry()
            inserted = self.state.enqueue(result.messages)
            if result.messages:
                logger.info(
                    "Drained %d Raft message(s), %d new",
                    len(result.messages),
                    inserted,
                )
            await self._process_pending()
            if not result.has_more:
                return

    async def _check_messages_with_retry(self) -> RaftCheckResult:
        """Retry only transport-safe inbox reads with bounded backoff."""

        attempts = self.config.inbox_transport_retry_attempts
        delay = self.config.inbox_transport_retry_base_seconds
        for attempt in range(1, attempts + 1):
            try:
                result = await self.raft.check_messages()
            except asyncio.CancelledError:
                raise
            except RaftTransportError as exc:
                if attempt >= attempts:
                    raise
                logger.warning(
                    "Raft inbox transport failed (attempt %d/%d); retrying in "
                    "%.1fs: %s",
                    attempt,
                    attempts,
                    delay,
                    exc,
                )
                if delay:
                    await asyncio.sleep(delay)
                delay = min(
                    max(delay * 2, self.config.inbox_transport_retry_base_seconds),
                    self.config.inbox_transport_retry_max_seconds,
                )
            else:
                if attempt > 1:
                    logger.info(
                        "Raft inbox transport recovered on attempt %d/%d",
                        attempt,
                        attempts,
                    )
                return result

        raise AssertionError("unreachable")

    async def _process_pending(self) -> None:
        for message in self.state.pending():
            try:
                await self._process_message(message)
            except asyncio.CancelledError:
                raise
            except RaftDeliveryUnknownError as exc:
                self.activity.emit(
                    "RuntimeError",
                    session_id=self.state.get_session(
                        message.reply_target, self.config.workspace
                    ),
                    status="error",
                    error_class=type(exc).__name__,
                )
                self.state.mark_delivery_unknown(message.key, str(exc))
                logger.error(
                    "Raft delivery state is unknown for message %s; "
                    "quarantined without automatic resend to avoid a duplicate",
                    message.key,
                )
            except Exception as exc:
                self.activity.emit(
                    "RuntimeError",
                    session_id=self.state.get_session(
                        message.reply_target, self.config.workspace
                    ),
                    status="error",
                    error_class=type(exc).__name__,
                )
                exhausted = self.state.mark_failed(
                    message.key,
                    f"{type(exc).__name__}: {exc}",
                    max_attempts=self.config.max_message_attempts,
                )
                logger.exception("Failed to process Raft message %s", message.key)
                if exhausted:
                    logger.error(
                        "Raft message %s reached the retry limit (%d) and was "
                        "moved to failed state",
                        message.key,
                        self.config.max_message_attempts,
                    )
                # A broken stdio transport otherwise leaves every later retry
                # attached to the same dead ACP connection. Session ids live in
                # SQLite and will be loaded again after reconnecting.
                with suppress(Exception):
                    await self.acp.close()
                    await self.acp.open()

    async def _process_message(self, message: PendingMessage) -> None:
        if message.reply_target.startswith("agent-event:"):
            logger.warning(
                "Skipping Raft third-party event %s because it has no reply target",
                message.message_id,
            )
            self.state.mark_done(message.key)
            return
        response = message.response_content
        if response is None:
            existing = self.state.get_session(
                message.reply_target, self.config.workspace
            )
            session_id = await self.acp.attach_or_create(existing)
            if session_id != existing:
                self.state.put_session(
                    message.reply_target, session_id, self.config.workspace
                )
            prompt = self._build_prompt(message)
            response = await self.acp.prompt(session_id, prompt)
            if not response:
                response = "DeerFlow completed the turn without a text response."
            # Commit the generated reply before attempting network delivery. If
            # Raft rejects a known-failed send, the retry reuses this outbox body
            # instead of running the expensive ACP turn and adding checkpoints.
            self.state.save_response(message.key, response)
        await self.raft.send_message(message.reply_target, response)
        self.state.mark_done(message.key)
        session_id = self.state.get_session(
            message.reply_target, self.config.workspace
        )
        self.activity.emit("Stop", session_id=session_id)
        logger.info(
            "Replied to Raft target %s",
            message.reply_target,
        )

    @staticmethod
    def _build_prompt(message: PendingMessage) -> str:
        return (
            "You are responding as an External Agent in Raft. Produce the reply "
            "that should be posted back to the same Raft conversation.\n\n"
            f"Raft sender: {message.sender}\n"
            f"Raft sender type: {message.sender_type}\n"
            f"Raft target: {message.target}\n"
            f"Raft message id: {message.message_id}\n\n"
            "Message:\n"
            f"{message.content}"
        )

    async def _bridge_supervisor(self) -> None:
        while not self._stop_requested.is_set():
            try:
                process = await self.raft.start_bridge(
                    expected_agent_id=self.config.raft_agent_id,
                    wake_endpoint=self.wake.wake_endpoint,
                    wake_token=self.config.wake_token,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Failed to start Raft bridge; retrying in %.1fs",
                    self.config.bridge_restart_seconds,
                )
                await asyncio.sleep(self.config.bridge_restart_seconds)
                continue
            stdout_task = asyncio.create_task(
                self._log_pipe(process.stdout, logging.INFO, "Raft bridge")
            )
            stderr_task = asyncio.create_task(
                self._log_pipe(process.stderr, logging.WARNING, "Raft bridge")
            )
            try:
                return_code = await process.wait()
            except asyncio.CancelledError:
                with suppress(ProcessLookupError):
                    process.terminate()
                with suppress(ProcessLookupError):
                    await process.wait()
                raise
            finally:
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            if self._stop_requested.is_set():
                return
            logger.warning(
                "Raft bridge exited with code %s; restarting in %.1fs",
                return_code,
                self.config.bridge_restart_seconds,
            )
            await asyncio.sleep(self.config.bridge_restart_seconds)

    @staticmethod
    async def _log_pipe(
        pipe: asyncio.StreamReader | None, level: int, prefix: str
    ) -> None:
        if pipe is None:
            return
        while line := await pipe.readline():
            logger.log(level, "%s: %s", prefix, line.decode(errors="replace").rstrip())
