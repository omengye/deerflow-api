from __future__ import annotations

from pathlib import Path

import pytest
from raft_deerflow_adapter.app import AdapterApp
from raft_deerflow_adapter.config import AdapterConfig
from raft_deerflow_adapter.models import RaftCheckResult, RaftMessage
from raft_deerflow_adapter.raft_cli import RaftCLIError, RaftTransportError


class _FakeACP:
    def __init__(self) -> None:
        self.prompt_calls = 0

    async def attach_or_create(self, _existing: str | None) -> str:
        return "session-1"

    async def prompt(self, _session_id: str, _prompt: str) -> str:
        self.prompt_calls += 1
        return "saved reply"

    async def close(self) -> None:
        return None

    async def open(self) -> None:
        return None


class _RetryingRaft:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.send_calls = 0

    async def send_message(self, _target: str, content: str) -> dict[str, object]:
        assert content == "saved reply"
        self.send_calls += 1
        if self.send_calls <= self.failures:
            raise RaftCLIError("known failed send")
        return {"status": "queued"}


class _RetryingInboxRaft:
    def __init__(self, failures: int, *, error: Exception | None = None) -> None:
        self.failures = failures
        self.error = error or RaftTransportError("temporary transport failure")
        self.check_calls = 0

    async def check_messages(self) -> RaftCheckResult:
        self.check_calls += 1
        if self.check_calls <= self.failures:
            raise self.error
        return RaftCheckResult(messages=[])


def _app(tmp_path: Path, *, max_attempts: int = 5) -> AdapterApp:
    config = AdapterConfig(
        raft_profile="test",
        raft_agent_id="agent-1",
        deerflow_command="fake-deerflow",
        workspace=tmp_path,
        state_path=tmp_path / "adapter.sqlite3",
        max_message_attempts=max_attempts,
        inbox_transport_retry_attempts=3,
        inbox_transport_retry_base_seconds=0,
        inbox_transport_retry_max_seconds=0,
    )
    return AdapterApp(config)


def _enqueue(app: AdapterApp) -> None:
    app.state.enqueue(
        [
            RaftMessage(
                target="#general",
                message_id="deadbeef",
                timestamp="2026-09-02 01:02:03Z",
                sender_type="human",
                sender="@human",
                content="hello",
            )
        ]
    )


@pytest.mark.asyncio
async def test_send_retry_reuses_persisted_response(tmp_path: Path) -> None:
    app = _app(tmp_path)
    acp = _FakeACP()
    raft = _RetryingRaft(failures=1)
    app.acp = acp  # type: ignore[assignment]
    app.raft = raft  # type: ignore[assignment]
    _enqueue(app)
    try:
        await app._process_pending()
        pending_row = app.state._conn().execute(
            "SELECT status, attempts, response_content FROM inbox_messages"
        ).fetchone()
        assert tuple(pending_row) == ("pending", 1, "saved reply")

        await app._process_pending()

        assert acp.prompt_calls == 1
        assert raft.send_calls == 2
        row = app.state._conn().execute(
            "SELECT status, attempts, response_content FROM inbox_messages"
        ).fetchone()
        assert tuple(row) == ("done", 1, None)
    finally:
        app.state.close()


@pytest.mark.asyncio
async def test_retry_limit_stops_further_delivery_attempts(tmp_path: Path) -> None:
    app = _app(tmp_path, max_attempts=2)
    acp = _FakeACP()
    raft = _RetryingRaft(failures=10)
    app.acp = acp  # type: ignore[assignment]
    app.raft = raft  # type: ignore[assignment]
    _enqueue(app)
    try:
        await app._process_pending()
        await app._process_pending()
        await app._process_pending()

        assert acp.prompt_calls == 1
        assert raft.send_calls == 2
        row = app.state._conn().execute(
            "SELECT status, attempts FROM inbox_messages"
        ).fetchone()
        assert tuple(row) == ("failed", 2)
    finally:
        app.state.close()


@pytest.mark.asyncio
async def test_inbox_transport_failure_retries_immediately(tmp_path: Path) -> None:
    app = _app(tmp_path)
    raft = _RetryingInboxRaft(failures=2)
    app.raft = raft  # type: ignore[assignment]
    try:
        await app.drain_once()
        assert raft.check_calls == 3
    finally:
        app.state.close()


@pytest.mark.asyncio
async def test_inbox_transport_retry_is_bounded(tmp_path: Path) -> None:
    app = _app(tmp_path)
    raft = _RetryingInboxRaft(failures=10)
    app.raft = raft  # type: ignore[assignment]
    try:
        with pytest.raises(RaftTransportError):
            await app.drain_once()
        assert raft.check_calls == 3
    finally:
        app.state.close()


@pytest.mark.asyncio
async def test_non_transport_inbox_error_is_not_retried(tmp_path: Path) -> None:
    app = _app(tmp_path)
    raft = _RetryingInboxRaft(
        failures=1,
        error=RaftCLIError("authentication failed"),
    )
    app.raft = raft  # type: ignore[assignment]
    try:
        with pytest.raises(RaftCLIError, match="authentication failed"):
            await app.drain_once()
        assert raft.check_calls == 1
    finally:
        app.state.close()
