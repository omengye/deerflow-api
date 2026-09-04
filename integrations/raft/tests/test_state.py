from __future__ import annotations

import sqlite3
from pathlib import Path

from raft_deerflow_adapter.models import RaftMessage
from raft_deerflow_adapter.state import AdapterState


def test_existing_unknown_delivery_is_quarantined_on_open(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite3"
    message = RaftMessage(
        target="#general",
        message_id="deadbeef",
        timestamp="2026-09-02 01:02:03Z",
        sender_type="human",
        sender="@human",
        content="hello",
    )
    state = AdapterState(state_path)
    state.enqueue([message])
    state.mark_failed(
        message.key,
        "Delivery state is UNKNOWN and not retryable. Do not resend on this evidence.",
        max_attempts=5,
    )
    state.close()

    reopened = AdapterState(state_path)
    try:
        assert reopened.pending() == []
        row = reopened._conn().execute(
            "SELECT status FROM inbox_messages WHERE message_key = ?", (message.key,)
        ).fetchone()
        assert row["status"] == "delivery_unknown"
    finally:
        reopened.close()


def test_generated_response_is_persisted_for_delivery_retry(tmp_path: Path) -> None:
    state_path = tmp_path / "state.sqlite3"
    message = RaftMessage(
        target="#general",
        message_id="feedface",
        timestamp="2026-09-02 02:03:04Z",
        sender_type="human",
        sender="@human",
        content="hello",
    )
    state = AdapterState(state_path)
    state.enqueue([message])
    state.save_response(message.key, "durable reply")
    state.close()

    reopened = AdapterState(state_path)
    try:
        pending = reopened.pending()
        assert len(pending) == 1
        assert pending[0].response_content == "durable reply"
    finally:
        reopened.close()


def test_enqueue_deduplicates_legacy_dm_alias_rows(tmp_path: Path) -> None:
    state = AdapterState(tmp_path / "state.sqlite3")
    common = {
        "message_id": "deadbeef",
        "timestamp": "2026-09-02 02:03:04Z",
        "sender_type": "human",
        "sender": "@human",
        "content": "hello",
    }
    legacy_alias = RaftMessage(target="dm:@deerflow:cafebabe", **common)
    peer_alias = RaftMessage(target="dm:@human:cafebabe", **common)
    try:
        assert state.enqueue([legacy_alias, peer_alias]) == 1
        pending = state.pending()
        assert len(pending) == 1
        assert pending[0].reply_target == "dm:@human:cafebabe"
    finally:
        state.close()


def test_open_migrates_legacy_state_database(tmp_path: Path) -> None:
    state_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(state_path)
    connection.execute(
        """
        CREATE TABLE inbox_messages (
            message_key TEXT PRIMARY KEY,
            target TEXT NOT NULL,
            reply_target TEXT NOT NULL,
            message_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            sender_type TEXT NOT NULL,
            sender TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        )
        """
    )
    connection.commit()
    connection.close()

    state = AdapterState(state_path)
    try:
        columns = {
            row["name"]
            for row in state._conn().execute("PRAGMA table_info(inbox_messages)")
        }
        assert "response_content" in columns
    finally:
        state.close()


def test_message_moves_to_failed_after_retry_limit(tmp_path: Path) -> None:
    state = AdapterState(tmp_path / "state.sqlite3")
    message = RaftMessage(
        target="#general",
        message_id="badc0ffe",
        timestamp="2026-09-02 03:04:05Z",
        sender_type="human",
        sender="@human",
        content="hello",
    )
    try:
        state.enqueue([message])
        assert not state.mark_failed(message.key, "first", max_attempts=2)
        assert state.mark_failed(message.key, "second", max_attempts=2)
        assert state.pending() == []
        row = state._conn().execute(
            "SELECT status, attempts FROM inbox_messages WHERE message_key = ?",
            (message.key,),
        ).fetchone()
        assert tuple(row) == ("failed", 2)
    finally:
        state.close()
