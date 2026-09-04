from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from raft_deerflow_adapter.raft_cli import (
    RaftCLI,
    RaftDeliveryUnknownError,
    RaftTransportError,
    parse_message_check,
)
from raft_deerflow_adapter.models import RaftMessage


def test_parse_empty_inbox() -> None:
    result = parse_message_check("No new inbox messages.\n")
    assert result.messages == []
    assert result.has_more is False


def test_parse_multiline_messages_and_thread_targets() -> None:
    output = """[target=#general msg=deadbeef time=2026-09-01 01:02:03Z type=human] @alice: First line
second line
[target=#general:cafebabe msg=0123abcd time=2026-09-01 01:03:04Z type=agent] @bob — Builder: Existing thread
More messages are pending. Run `raft message check` again.
"""
    result = parse_message_check(output)
    assert result.has_more is True
    assert len(result.messages) == 2
    assert result.messages[0].content == "First line\nsecond line"
    assert result.messages[0].reply_target == "#general:deadbeef"
    assert result.messages[1].reply_target == "#general:cafebabe"
    assert result.messages[1].sender == "@bob — Builder"


def test_dm_aliases_share_reply_target_and_message_key() -> None:
    common = {
        "message_id": "deadbeef",
        "timestamp": "2026-09-02 01:02:03Z",
        "sender_type": "human",
        "sender": "@alice",
        "content": "hello",
    }
    local_alias = RaftMessage(target="dm:@deerflow:cafebabe", **common)
    peer_alias = RaftMessage(target="dm:@alice:cafebabe", **common)

    assert local_alias.reply_target == "dm:@alice:cafebabe"
    assert peer_alias.reply_target == "dm:@alice:cafebabe"
    assert local_alias.key == peer_alias.key


def test_unthreaded_dm_reply_stays_in_main_dm() -> None:
    message = RaftMessage(
        target="dm:@deerflow",
        message_id="deadbeef",
        timestamp="2026-09-02 01:02:03Z",
        sender_type="agent",
        sender="@alice — Alice Agent",
        content="hello",
    )

    assert message.reply_target == "dm:@alice"


async def test_send_raises_non_retryable_delivery_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "fake_raft_cli.py"
    monkeypatch.setenv("FAKE_RAFT_INBOX", str(tmp_path / "inbox.txt"))
    monkeypatch.setenv("FAKE_RAFT_CHECKED", str(tmp_path / "checked"))
    monkeypatch.setenv("FAKE_RAFT_READY", str(tmp_path / "ready"))
    monkeypatch.setenv("FAKE_RAFT_SENT", str(tmp_path / "sent.jsonl"))
    monkeypatch.setenv("FAKE_RAFT_SEND_MODE", "delivery_unknown")
    cli = RaftCLI(sys.executable, [str(fixture)], "test-profile")

    with pytest.raises(RaftDeliveryUnknownError, match="unknown delivery state"):
        await cli.send_message("#general:deadbeef", "reply body")


async def test_check_classifies_agent_api_transport_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "fake_raft_cli.py"
    monkeypatch.setenv("FAKE_RAFT_INBOX", str(tmp_path / "inbox.txt"))
    monkeypatch.setenv("FAKE_RAFT_CHECKED", str(tmp_path / "checked"))
    monkeypatch.setenv("FAKE_RAFT_READY", str(tmp_path / "ready"))
    monkeypatch.setenv("FAKE_RAFT_SENT", str(tmp_path / "sent.jsonl"))
    monkeypatch.setenv("FAKE_RAFT_CHECK_MODE", "transport_error")
    cli = RaftCLI(sys.executable, [str(fixture)], "test-profile")

    with pytest.raises(RaftTransportError, match="transport failed"):
        await cli.check_messages()


async def test_top_level_dm_send_confirms_intentional_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "fake_raft_cli.py"
    sent = tmp_path / "sent.jsonl"
    monkeypatch.setenv("FAKE_RAFT_INBOX", str(tmp_path / "inbox.txt"))
    monkeypatch.setenv("FAKE_RAFT_CHECKED", str(tmp_path / "checked"))
    monkeypatch.setenv("FAKE_RAFT_READY", str(tmp_path / "ready"))
    monkeypatch.setenv("FAKE_RAFT_SENT", str(sent))
    cli = RaftCLI(sys.executable, [str(fixture)], "test-profile")

    await cli.send_message("dm:@alice", "reply body")

    payload = json.loads(sent.read_text(encoding="utf-8"))
    assert payload["target"] == "dm:@alice"
    assert "--target-confirmed" in payload["args"]


async def test_send_confirms_explicitly_held_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "fake_raft_cli.py"
    sent = tmp_path / "sent.jsonl"
    monkeypatch.setenv("FAKE_RAFT_INBOX", str(tmp_path / "inbox.txt"))
    monkeypatch.setenv("FAKE_RAFT_CHECKED", str(tmp_path / "checked"))
    monkeypatch.setenv("FAKE_RAFT_READY", str(tmp_path / "ready"))
    monkeypatch.setenv("FAKE_RAFT_SENT", str(sent))
    monkeypatch.setenv("FAKE_RAFT_SEND_MODE", "draft_held")
    cli = RaftCLI(sys.executable, [str(fixture)], "test-profile")

    await cli.send_message("dm:@alice", "durable reply")

    payload = json.loads(sent.read_text(encoding="utf-8"))
    assert payload["content"] == "durable reply"
    assert "--send-draft" in payload["args"]
