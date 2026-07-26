"""Regression tests for deerflow.mcp.oauth cross-event-loop locking.

``OAuthTokenManager`` is reused by sync tool-call paths that create a
brand-new event loop *per call* (see
``deerflow.tools.sync.make_sync_tool_wrapper`` and
``deerflow.mcp.cache.get_cached_mcp_tools``, both of which wrap the async
refresh path in ``asyncio.run(...)`` on a worker thread pool). A single
``asyncio.Lock`` created once at manager construction time binds to
whichever event loop first awaits it; a second loop touching that lock
raises a cross-loop binding error (or hangs). These tests exercise the
fix: a lock scoped per (server, running event loop) instead of one lock
shared across the manager's lifetime.

All HTTP calls are mocked out by monkeypatching ``_fetch_token`` directly —
no real network access.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest

from deerflow.config.extensions_config import McpOAuthConfig
from deerflow.mcp.oauth import OAuthTokenManager, _OAuthToken

SERVER = "test-server"


def _make_manager() -> OAuthTokenManager:
    oauth = McpOAuthConfig(
        token_url="https://example.invalid/token",
        client_id="client",
        client_secret="secret",
        refresh_skew_seconds=60,
    )
    return OAuthTokenManager({SERVER: oauth})


def _fresh_token(ttl_seconds: int = 3600) -> _OAuthToken:
    return _OAuthToken(
        access_token="fresh-access-token",
        token_type="Bearer",
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
    )


def _expired_token() -> _OAuthToken:
    return _OAuthToken(
        access_token="stale-access-token",
        token_type="Bearer",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )


# ---------------------------------------------------------------------------
# (c) Expiry / refresh_skew regression coverage (pre-existing behavior).
# ---------------------------------------------------------------------------


def test_unknown_server_returns_none() -> None:
    manager = _make_manager()
    assert asyncio.run(manager.get_authorization_header("not-configured")) is None


def test_cached_non_expiring_token_short_circuits_without_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_manager()
    manager._store_token(SERVER, _fresh_token(ttl_seconds=3600))

    async def fail_fetch_token(self, oauth):
        raise AssertionError("must not refetch a still-valid, non-expiring token")

    monkeypatch.setattr(OAuthTokenManager, "_fetch_token", fail_fetch_token)

    header = asyncio.run(manager.get_authorization_header(SERVER))
    assert header == "Bearer fresh-access-token"


def test_expired_cached_token_triggers_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_manager()
    manager._store_token(SERVER, _expired_token())
    fetch_calls = 0

    async def fake_fetch_token(self, oauth):
        nonlocal fetch_calls
        fetch_calls += 1
        return _fresh_token()

    monkeypatch.setattr(OAuthTokenManager, "_fetch_token", fake_fetch_token)

    header = asyncio.run(manager.get_authorization_header(SERVER))
    assert header == "Bearer fresh-access-token"
    assert fetch_calls == 1


def test_token_within_refresh_skew_triggers_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_manager()
    # Expires in 10s but refresh_skew_seconds=60 on the config -> must be
    # treated as expiring, not reused as-is.
    manager._store_token(SERVER, _fresh_token(ttl_seconds=10))
    fetch_calls = 0

    async def fake_fetch_token(self, oauth):
        nonlocal fetch_calls
        fetch_calls += 1
        return _fresh_token(ttl_seconds=3600)

    monkeypatch.setattr(OAuthTokenManager, "_fetch_token", fake_fetch_token)

    header = asyncio.run(manager.get_authorization_header(SERVER))
    assert header is not None
    assert fetch_calls == 1


# ---------------------------------------------------------------------------
# (b) Same event loop: concurrent callers coalesce onto a single in-flight
#     refresh (original single-fetch semantics preserved).
# ---------------------------------------------------------------------------


def test_same_loop_concurrent_refresh_coalesces_into_one_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_manager()
    fetch_calls = 0

    async def fake_fetch_token(self, oauth):
        nonlocal fetch_calls
        fetch_calls += 1
        await asyncio.sleep(0.05)  # widen the window so callers actually overlap
        return _fresh_token()

    monkeypatch.setattr(OAuthTokenManager, "_fetch_token", fake_fetch_token)

    async def scenario() -> list[str | None]:
        return await asyncio.gather(
            manager.get_authorization_header(SERVER),
            manager.get_authorization_header(SERVER),
            manager.get_authorization_header(SERVER),
        )

    results = asyncio.run(scenario())

    assert all(r == "Bearer fresh-access-token" for r in results)
    assert fetch_calls == 1


# ---------------------------------------------------------------------------
# (a) Two different event loops (two threads, each its own asyncio.run) must
#     not raise a cross-loop lock-binding error and must not deadlock.
# ---------------------------------------------------------------------------


def _run_get_header_in_new_loop(
    manager: OAuthTokenManager,
    server_name: str,
    results: list[str | None],
    errors: list[BaseException | None],
    index: int,
) -> None:
    # Mirrors deerflow.tools.sync.make_sync_tool_wrapper: a brand-new event
    # loop is created (and destroyed) for this single call.
    try:
        results[index] = asyncio.run(manager.get_authorization_header(server_name))
    except BaseException as exc:  # noqa: BLE001 - capture the cross-loop error itself
        errors[index] = exc


def test_cross_loop_refresh_does_not_deadlock_or_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_manager()
    fetch_calls = 0
    fetch_calls_guard = threading.Lock()

    async def fake_fetch_token(self, oauth):
        nonlocal fetch_calls
        with fetch_calls_guard:
            fetch_calls += 1
        await asyncio.sleep(0.05)  # widen the window for cross-loop overlap
        return _fresh_token()

    monkeypatch.setattr(OAuthTokenManager, "_fetch_token", fake_fetch_token)

    results: list[str | None] = [None, None]
    errors: list[BaseException | None] = [None, None]
    # Plain daemon threads on purpose, NOT concurrent.futures.ThreadPoolExecutor:
    # if the lock genuinely deadlocks, ThreadPoolExecutor's shutdown-on-exit
    # (and its atexit handler) blocks joining the wedged worker thread
    # forever, hanging the whole test process instead of just failing this
    # test. Daemon threads + Thread.join(timeout=...) fail fast instead.
    threads = [
        threading.Thread(target=_run_get_header_in_new_loop, args=(manager, SERVER, results, errors, i), daemon=True)
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    for i, t in enumerate(threads):
        assert not t.is_alive(), f"thread {i} did not finish within 5s -- looks deadlocked"
    for i, err in enumerate(errors):
        assert err is None, f"thread {i} raised: {err!r}"

    assert results[0] == "Bearer fresh-access-token"
    assert results[1] == "Bearer fresh-access-token"
    # Benign, documented tradeoff: concurrent refreshes from different loops
    # don't serialize against each other, so this may be 1 or 2.
    assert fetch_calls in (1, 2)

    # A third call on yet another fresh loop must still succeed, proving no
    # lock was left permanently wedged by the concurrent access above.
    third: list[str | None] = [None]
    third_errors: list[BaseException | None] = [None]
    third_thread = threading.Thread(
        target=_run_get_header_in_new_loop, args=(manager, SERVER, third, third_errors, 0), daemon=True
    )
    third_thread.start()
    third_thread.join(timeout=5)
    assert not third_thread.is_alive(), "follow-up call did not finish within 5s -- lock left wedged"
    assert third_errors[0] is None, f"follow-up call raised: {third_errors[0]!r}"
    assert third[0] == "Bearer fresh-access-token"


def test_cross_loop_sequential_refresh_reuses_cached_token(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_manager()
    fetch_calls = 0

    async def fake_fetch_token(self, oauth):
        nonlocal fetch_calls
        fetch_calls += 1
        return _fresh_token(ttl_seconds=3600)

    monkeypatch.setattr(OAuthTokenManager, "_fetch_token", fake_fetch_token)

    def run_once(results: list[str | None], errors: list[BaseException | None]) -> None:
        _run_get_header_in_new_loop(manager, SERVER, results, errors, 0)

    first_result: list[str | None] = [None]
    first_errors: list[BaseException | None] = [None]
    t1 = threading.Thread(target=run_once, args=(first_result, first_errors), daemon=True)
    t1.start()
    t1.join(timeout=5)
    assert not t1.is_alive(), "first call did not finish within 5s -- looks deadlocked"
    assert first_errors[0] is None, f"first call raised: {first_errors[0]!r}"

    second_result: list[str | None] = [None]
    second_errors: list[BaseException | None] = [None]
    t2 = threading.Thread(target=run_once, args=(second_result, second_errors), daemon=True)
    t2.start()
    t2.join(timeout=5)
    assert not t2.is_alive(), "second call did not finish within 5s -- looks deadlocked"
    assert second_errors[0] is None, f"second call raised: {second_errors[0]!r}"

    assert first_result[0] == second_result[0] == "Bearer fresh-access-token"
    # The token cache (guarded by _state_lock, independent of the per-loop
    # lock) is shared across loops, so the second call must not refetch.
    assert fetch_calls == 1
