"""Memory update queue with debounce mechanism."""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from deerflow.config.memory_config import get_memory_config

logger = logging.getLogger(__name__)


@dataclass
class ConversationContext:
    """Context for a conversation to be processed for memory update."""

    thread_id: str
    messages: list[Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    agent_name: str | None = None
    user_id: str | None = None
    correction_detected: bool = False
    reinforcement_detected: bool = False


class MemoryUpdateQueue:
    """Queue for memory updates with debounce mechanism.

    This queue collects conversation contexts and processes them after
    a configurable debounce period. Multiple conversations received within
    the debounce window are batched together.
    """

    def __init__(self):
        """Initialize the memory update queue."""
        self._queue: list[ConversationContext] = []
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._timer: threading.Timer | None = None
        self._processing = False
        self._process_starting = False
        self._process_requested = False
        self._paused = False

    def add(
        self,
        thread_id: str,
        messages: list[Any],
        agent_name: str | None = None,
        user_id: str | None = None,
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
    ) -> None:
        """Add a conversation to the update queue.

        Args:
            thread_id: The thread ID.
            messages: The conversation messages.
            agent_name: If provided, memory is stored per-agent. If None, uses global memory.
            correction_detected: Whether recent turns include an explicit correction signal.
            reinforcement_detected: Whether recent turns include a positive reinforcement signal.
        """
        config = get_memory_config()
        if not config.enabled:
            return

        with self._lock:
            self._enqueue_locked(
                thread_id=thread_id,
                messages=messages,
                agent_name=agent_name,
                user_id=user_id,
                correction_detected=correction_detected,
                reinforcement_detected=reinforcement_detected,
            )
            if self._paused:
                self._condition.notify_all()
            elif self._processing:
                self._process_requested = True
            else:
                self._reset_timer()

        logger.info("Memory update queued for thread %s, queue size: %d", thread_id, len(self._queue))

    def add_nowait(
        self,
        thread_id: str,
        messages: list[Any],
        agent_name: str | None = None,
        user_id: str | None = None,
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
    ) -> None:
        """Add a conversation and start processing immediately in the background."""
        config = get_memory_config()
        if not config.enabled:
            return

        with self._lock:
            self._enqueue_locked(
                thread_id=thread_id,
                messages=messages,
                agent_name=agent_name,
                user_id=user_id,
                correction_detected=correction_detected,
                reinforcement_detected=reinforcement_detected,
            )
            if self._paused:
                self._condition.notify_all()
            elif self._processing:
                self._process_requested = True
            else:
                self._schedule_timer(0)

        logger.info("Memory update queued for immediate processing on thread %s, queue size: %d", thread_id, len(self._queue))

    def _enqueue_locked(
        self,
        *,
        thread_id: str,
        messages: list[Any],
        agent_name: str | None,
        user_id: str | None = None,
        correction_detected: bool = False,
        reinforcement_detected: bool = False,
    ) -> None:
        existing_context = next(
            (
                context
                for context in self._queue
                if context.thread_id == thread_id
                and context.agent_name == agent_name
                and context.user_id == user_id
            ),
            None,
        )
        merged_correction_detected = correction_detected or (existing_context.correction_detected if existing_context is not None else False)
        merged_reinforcement_detected = reinforcement_detected or (existing_context.reinforcement_detected if existing_context is not None else False)
        merged_messages = self._merge_messages(existing_context.messages if existing_context is not None else [], messages)
        context = ConversationContext(
            thread_id=thread_id,
            messages=merged_messages,
            agent_name=agent_name,
            user_id=user_id,
            correction_detected=merged_correction_detected,
            reinforcement_detected=merged_reinforcement_detected,
        )

        self._queue = [
            context
            for context in self._queue
            if not (
                context.thread_id == thread_id
                and context.agent_name == agent_name
                and context.user_id == user_id
            )
        ]
        self._queue.append(context)

    @staticmethod
    def _merge_messages(existing: list[Any], new_messages: list[Any]) -> list[Any]:
        """Merge queued message snapshots, de-duplicating messages with stable ids."""
        merged = list(existing)
        seen_ids = {message_id for message_id in (getattr(message, "id", None) for message in merged) if message_id}
        for message in new_messages:
            message_id = getattr(message, "id", None)
            if message_id and message_id in seen_ids:
                continue
            merged.append(message)
            if message_id:
                seen_ids.add(message_id)
        return merged

    def _reset_timer(self) -> None:
        """Reset the debounce timer."""
        config = get_memory_config()
        self._schedule_timer(config.debounce_seconds)

        logger.debug("Memory update timer set for %ss", config.debounce_seconds)

    def _schedule_timer(self, delay_seconds: float) -> None:
        """Schedule queue processing after the provided delay."""
        # Cancel existing timer if any
        if self._timer is not None:
            self._timer.cancel()

        self._timer = threading.Timer(
            delay_seconds,
            self._process_queue,
        )
        self._timer.daemon = True
        self._timer.start()

    def _process_queue(self, *, force: bool = False, claimed_start: bool = False) -> None:
        """Process all queued conversation contexts."""
        # Import here to avoid circular dependency and resolve the active
        # backend at processing time (configuration may have been reloaded
        # while an item was waiting in the debounce window).
        from deerflow.agents.memory.manager import memory_manager_lease

        with self._lock:
            if claimed_start:
                self._process_starting = False
            if self._paused and not force:
                self._timer = None
                self._condition.notify_all()
                return
            if self._processing:
                self._process_requested = True
                self._condition.notify_all()
                return

            if not self._queue:
                self._timer = None
                self._condition.notify_all()
                return

            self._processing = True
            self._process_requested = False
            contexts_to_process = self._queue.copy()
            self._queue.clear()
            self._timer = None
            self._condition.notify_all()

        logger.info("Processing %d queued memory updates", len(contexts_to_process))

        try:
            with memory_manager_lease() as manager:
                for context in contexts_to_process:
                    try:
                        logger.info("Updating memory for thread %s", context.thread_id)
                        success = manager.add(
                            messages=context.messages,
                            thread_id=context.thread_id,
                            agent_name=context.agent_name,
                            user_id=context.user_id,
                            correction_detected=context.correction_detected,
                            reinforcement_detected=context.reinforcement_detected,
                        )
                        if success:
                            logger.info("Memory updated successfully for thread %s", context.thread_id)
                        else:
                            logger.warning("Memory update skipped/failed for thread %s", context.thread_id)
                    except Exception as exc:
                        # Custom backends control exception text and may include
                        # credentials.  Keep logs useful without copying it.
                        logger.error(
                            "Error updating memory for thread %s (%s)",
                            context.thread_id,
                            type(exc).__name__,
                        )

                    # Small delay between updates to avoid rate limiting
                    if len(contexts_to_process) > 1:
                        time.sleep(0.5)

        finally:
            with self._lock:
                self._processing = False
                should_process_pending = bool(self._queue) and self._process_requested
                self._process_requested = False
                if should_process_pending and not self._paused:
                    self._schedule_timer(0)
                self._condition.notify_all()

    def flush(self, timeout_seconds: float | None = None) -> bool:
        """Process all work queued before or during this flush.

        If another worker is already writing memory, wait for it and then drain
        anything that arrived meanwhile.  ``False`` means the timeout expired
        while a write was still active; the active worker is left intact and
        will continue processing in the background.
        """
        deadline = (
            time.monotonic() + timeout_seconds
            if timeout_seconds is not None
            else None
        )

        while True:
            with self._condition:
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None

                while self._processing or self._process_starting:
                    # Ensure work appended during the active batch is picked up
                    # immediately when that worker finishes.
                    self._process_requested = True
                    remaining = (
                        deadline - time.monotonic()
                        if deadline is not None
                        else None
                    )
                    if remaining is not None and remaining <= 0:
                        return False
                    self._condition.wait(timeout=remaining)
                    if self._timer is not None:
                        self._timer.cancel()
                        self._timer = None

                if not self._queue:
                    return True

                # Never execute backend I/O in the caller: a synchronous
                # manager.add() cannot be interrupted.  A daemon worker lets
                # the condition wait enforce the configured timeout while an
                # overrun continues safely with its manager lease.
                self._process_starting = True
                worker = threading.Thread(
                    target=self._process_queue,
                    kwargs={"force": True, "claimed_start": True},
                    daemon=True,
                    name="deerflow-memory-flush",
                )
                try:
                    worker.start()
                except Exception:
                    self._process_starting = False
                    self._condition.notify_all()
                    raise

    def pause(self) -> None:
        """Pause timer-driven processing while continuing to accept writes."""
        with self._condition:
            self._paused = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._condition.notify_all()

    def resume(self) -> None:
        """Resume processing and schedule any writes held during a transition."""
        with self._condition:
            self._paused = False
            if self._queue and not self._processing:
                self._schedule_timer(0)
            elif self._queue and self._processing:
                self._process_requested = True
            self._condition.notify_all()

    def flush_nowait(self) -> None:
        """Start queue processing immediately in a background thread."""
        with self._lock:
            # Daemon thread: queued messages may be lost if the process exits
            # before _process_queue completes. Acceptable for best-effort memory updates.
            self._schedule_timer(0)

    def clear(self) -> None:
        """Clear the queue without processing.

        This is useful for testing.
        """
        with self._condition:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._queue.clear()
            self._process_requested = False
            self._condition.notify_all()

    @property
    def pending_count(self) -> int:
        """Get the number of pending updates."""
        with self._lock:
            return len(self._queue)

    @property
    def is_processing(self) -> bool:
        """Check if the queue is currently being processed."""
        with self._lock:
            return self._processing

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused


# Global singleton instance
_memory_queue: MemoryUpdateQueue | None = None
_queue_lock = threading.Lock()


def get_memory_queue() -> MemoryUpdateQueue:
    """Get the global memory update queue singleton.

    Returns:
        The memory update queue instance.
    """
    global _memory_queue
    with _queue_lock:
        if _memory_queue is None:
            _memory_queue = MemoryUpdateQueue()
        return _memory_queue


def reset_memory_queue() -> None:
    """Reset the global memory queue.

    This is useful for testing.
    """
    global _memory_queue
    with _queue_lock:
        if _memory_queue is not None:
            _memory_queue.clear()
        _memory_queue = None
