from dataclasses import dataclass
from typing import Any

import pytest

from deerflow.agents.memory.queue import ConversationContext, MemoryUpdateQueue


@dataclass
class _Msg:
    id: str | None
    type: str = "human"
    content: str = "content"


def _context(thread_id: str = "thread-1", *, agent_name: str | None = None) -> ConversationContext:
    return ConversationContext(thread_id=thread_id, messages=[_Msg("existing")], agent_name=agent_name)


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
