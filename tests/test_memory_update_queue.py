import threading
from dataclasses import dataclass
from typing import Any

import pytest

from deerflow.agents.memory.queue import ConversationContext, MemoryUpdateQueue


@dataclass
class _Msg:
    id: str | None
    type: str = "human"
    content: str = "content"


def _context(
    thread_id: str = "thread-1",
    *,
    agent_name: str | None = None,
    user_id: str | None = None,
) -> ConversationContext:
    return ConversationContext(
        thread_id=thread_id,
        messages=[_Msg("existing")],
        agent_name=agent_name,
        user_id=user_id,
    )


def test_cancel_by_agent_only_removes_the_requested_memory_scope() -> None:
    queue = MemoryUpdateQueue()
    queue._queue.extend(
        [
            _context("a-1", agent_name="agent-a", user_id="user-1"),
            _context("a-2", agent_name="agent-a", user_id="user-2"),
            _context("a-legacy", agent_name="agent-a"),
            _context("b-1", agent_name="agent-b", user_id="user-1"),
        ]
    )

    assert queue.cancel_by_agent("agent-a", user_id="user-1") == 1
    assert queue.cancel_by_agent("agent-a") == 1
    assert [context.thread_id for context in queue._queue] == ["a-2", "b-1"]


def test_cancel_by_agent_can_remove_all_users_for_agent_scoped_storage() -> None:
    queue = MemoryUpdateQueue()
    queue._queue.extend(
        [
            _context("a-1", agent_name="agent-a", user_id="user-1"),
            _context("a-2", agent_name="agent-a", user_id="user-2"),
            _context("b-1", agent_name="agent-b", user_id="user-1"),
        ]
    )

    assert queue.cancel_by_agent("agent-a", all_users=True) == 2
    assert [context.thread_id for context in queue._queue] == ["b-1"]


def test_cancel_by_agent_stops_timer_when_no_pending_work_remains() -> None:
    queue = MemoryUpdateQueue()

    class FakeTimer:
        cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    timer = FakeTimer()
    queue._timer = timer  # type: ignore[assignment]
    queue._queue.append(_context(agent_name="agent-a"))

    assert queue.cancel_by_agent("agent-a") == 1
    assert timer.cancelled is True
    assert queue._timer is None


def test_queue_merges_by_agent_and_thread_without_cross_agent_overwrite() -> None:
    queue = MemoryUpdateQueue()

    with queue._lock:
        queue._enqueue_locked(
            thread_id="thread-1",
            messages=[_Msg("a1")],
            agent_name="agent-a",
            correction_detected=False,
            reinforcement_detected=False,
        )
        queue._enqueue_locked(
            thread_id="thread-1",
            messages=[_Msg("b1")],
            agent_name="agent-b",
            correction_detected=False,
            reinforcement_detected=False,
        )

    assert queue.pending_count == 2
    contexts = {(context.agent_name, context.thread_id): context for context in queue._queue}
    assert ("agent-a", "thread-1") in contexts
    assert ("agent-b", "thread-1") in contexts


def test_queue_merges_same_context_messages_and_deduplicates_ids() -> None:
    queue = MemoryUpdateQueue()

    with queue._lock:
        queue._enqueue_locked(
            thread_id="thread-1",
            messages=[_Msg("m1"), _Msg("m2")],
            agent_name="agent-a",
            correction_detected=False,
            reinforcement_detected=False,
        )
        queue._enqueue_locked(
            thread_id="thread-1",
            messages=[_Msg("m2"), _Msg("m3")],
            agent_name="agent-a",
            correction_detected=True,
            reinforcement_detected=False,
        )

    assert queue.pending_count == 1
    context = queue._queue[0]
    assert [message.id for message in context.messages] == ["m1", "m2", "m3"]
    assert context.correction_detected is True


def test_process_queue_marks_pending_work_without_timer_spin(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = MemoryUpdateQueue()
    queue._processing = True
    queue._queue.append(_context())

    def fail_schedule(_delay_seconds: float) -> None:
        raise AssertionError("processing path must not schedule another immediate timer")

    monkeypatch.setattr(queue, "_schedule_timer", fail_schedule)

    queue._process_queue()

    assert queue._process_requested is True
    assert queue.pending_count == 1


def test_flush_waits_for_active_writer_and_drains_follow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = MemoryUpdateQueue()
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class BlockingManager:
        def add(self, **kwargs: Any) -> bool:
            calls.append(kwargs["thread_id"])
            if len(calls) == 1:
                started.set()
                assert release.wait(2)
            return True

    monkeypatch.setattr(
        "deerflow.agents.memory.manager.get_memory_manager",
        lambda: BlockingManager(),
    )
    with queue._lock:
        queue._queue.append(_context("first"))

    processor = threading.Thread(target=queue._process_queue)
    processor.start()
    assert started.wait(1)
    with queue._lock:
        queue._queue.append(_context("second"))

    result: list[bool] = []
    flusher = threading.Thread(target=lambda: result.append(queue.flush(2)))
    flusher.start()
    assert flusher.is_alive()

    release.set()
    processor.join(2)
    flusher.join(2)

    assert not processor.is_alive()
    assert not flusher.is_alive()
    assert result == [True]
    assert calls == ["first", "second"]
    assert queue.pending_count == 0


def test_flush_timeout_does_not_invalidate_active_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = MemoryUpdateQueue()
    started = threading.Event()
    release = threading.Event()

    class BlockingManager:
        def add(self, **_kwargs: Any) -> bool:
            started.set()
            assert release.wait(2)
            return True

    monkeypatch.setattr(
        "deerflow.agents.memory.manager.get_memory_manager",
        lambda: BlockingManager(),
    )
    with queue._lock:
        queue._queue.append(_context("first"))

    processor = threading.Thread(target=queue._process_queue)
    processor.start()
    assert started.wait(1)
    try:
        assert queue.flush(0.01) is False
        assert queue.is_processing is True
    finally:
        release.set()
        processor.join(2)

    assert not processor.is_alive()


def test_flush_timeout_is_enforced_when_queued_write_has_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = MemoryUpdateQueue()
    started = threading.Event()
    release = threading.Event()

    class BlockingManager:
        def add(self, **_kwargs: Any) -> bool:
            started.set()
            assert release.wait(2)
            return True

    monkeypatch.setattr(
        "deerflow.agents.memory.manager.get_memory_manager",
        lambda: BlockingManager(),
    )
    with queue._lock:
        queue._queue.append(_context("queued"))

    try:
        assert queue.flush(0.05) is False
        assert started.wait(1)
        assert queue.is_processing is True
    finally:
        release.set()

    assert queue.flush(1) is True
    assert queue.pending_count == 0


def test_queue_log_does_not_expose_backend_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue = MemoryUpdateQueue()
    secret = "custom-backend-secret"

    class BrokenManager:
        def add(self, **_kwargs: Any) -> bool:
            raise RuntimeError(secret)

    monkeypatch.setattr(
        "deerflow.agents.memory.manager.get_memory_manager",
        lambda: BrokenManager(),
    )
    with queue._lock:
        queue._queue.append(_context("redacted"))

    with caplog.at_level("ERROR"):
        assert queue.flush(1) is True

    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text


def test_pause_holds_new_writes_until_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = MemoryUpdateQueue()
    processed = threading.Event()

    class RecordingManager:
        def add(self, **_kwargs: Any) -> bool:
            processed.set()
            return True

    monkeypatch.setattr(
        "deerflow.agents.memory.manager.get_memory_manager",
        lambda: RecordingManager(),
    )

    queue.pause()
    queue.add_nowait("held", [_Msg("held")])
    assert queue.is_paused is True
    assert queue.pending_count == 1
    assert processed.wait(0.05) is False

    queue.resume()
    assert processed.wait(1)
    assert queue.flush(1) is True
    assert queue.pending_count == 0
