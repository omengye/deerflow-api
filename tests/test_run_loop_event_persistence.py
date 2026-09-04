from __future__ import annotations

from types import SimpleNamespace

from app.dependencies import _record_loop_event


def test_loop_events_are_bounded_deduplicated_and_attributed() -> None:
    record = SimpleNamespace(metadata={})
    event = {
        "type": "loop_warning",
        "message": "repeated call",
        "task_id": "task-1",
        "subagent_type": "general-purpose",
        "secret": "must-not-persist",
    }

    _record_loop_event(record, "loop_warning", event)
    _record_loop_event(record, "loop_warning", event)

    assert len(record.metadata["loop_events"]) == 1
    saved = record.metadata["loop_events"][0]
    assert saved["type"] == "loop_warning"
    assert saved["task_id"] == "task-1"
    assert saved["subagent_type"] == "general-purpose"
    assert "secret" not in saved


def test_custom_loop_event_is_recognized_and_message_is_bounded() -> None:
    record = SimpleNamespace(metadata={})

    _record_loop_event(
        record,
        "custom",
        {
            "type": "loop_hard_stop",
            "message": "x" * 3000,
            "reason": "tool_call_limit",
            "incomplete": True,
        },
    )
    _record_loop_event(record, "custom", {"type": "unrelated", "message": "no"})

    assert len(record.metadata["loop_events"]) == 1
    saved = record.metadata["loop_events"][0]
    assert len(saved["message"]) == 2000
    assert saved["reason"] == "tool_call_limit"
    assert saved["incomplete"] is True
