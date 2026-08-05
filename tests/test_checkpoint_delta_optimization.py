from __future__ import annotations

import asyncio
import importlib
import threading
from typing import Any

import pytest

from deerflow.runtime.checkpoint_cache.base import make_history_key
from deerflow.runtime.checkpoint_cache.memory import MemoryCheckpointHistoryCache
from deerflow.runtime.checkpoint_mode import (
    CheckpointModeMismatchError,
    CheckpointModeReconfigurationError,
    checkpoint_metadata_uses_delta,
    ensure_checkpoint_mode_compatible,
    freeze_checkpoint_channel_mode,
    freeze_checkpoint_snapshot_frequency,
    inject_checkpoint_mode,
    reset_checkpoint_mode_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_mode() -> None:
    reset_checkpoint_mode_for_tests()
    yield
    reset_checkpoint_mode_for_tests()


def _graph(mode: str, saver: Any, *, frequency: int = 10):
    # Some SDK isolation tests intentionally evict LangChain/LangGraph modules
    # from sys.modules. Import the schema factory at execution time so channel
    # class identities match the active StateGraph runtime.
    thread_state = importlib.reload(
        importlib.import_module("deerflow.agents.thread_state")
    )
    from langgraph.graph import END, START, StateGraph

    def respond(state):
        turns = len(
            [
                message
                for message in state.get("messages", [])
                if getattr(message, "type", None) == "human"
                or (isinstance(message, dict) and message.get("role") == "user")
            ]
        )
        return {"messages": [{"role": "assistant", "content": f"answer-{turns}"}]}

    builder = StateGraph(thread_state.get_thread_state_schema(mode, frequency))
    builder.add_node("respond", respond)
    builder.add_edge(START, "respond")
    builder.add_edge("respond", END)
    return builder.compile(checkpointer=saver)


def _run_two_turns(mode: str, saver: Any) -> tuple[Any, dict[str, Any]]:
    graph = _graph(mode, saver)
    config: dict[str, Any] = {"configurable": {"thread_id": f"thread-{mode}"}}
    inject_checkpoint_mode(config, mode)
    graph.invoke({"messages": [{"role": "user", "content": "one"}]}, config)
    result = graph.invoke({"messages": [{"role": "user", "content": "two"}]}, config)
    return graph, result


def _memory_saver():
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


def test_full_and_delta_materialize_identical_message_state() -> None:
    _full_graph, full = _run_two_turns("full", _memory_saver())
    _delta_graph, delta = _run_two_turns("delta", _memory_saver())

    assert [(message.type, message.content) for message in full["messages"]] == [
        (message.type, message.content) for message in delta["messages"]
    ]


def test_delta_storage_uses_marker_and_non_full_channel_values() -> None:
    saver = _memory_saver()
    graph = _graph("delta", saver, frequency=10)
    config: dict[str, Any] = {"configurable": {"thread_id": "delta-raw"}}
    inject_checkpoint_mode(config, "delta")

    graph.invoke({"messages": [{"role": "user", "content": "one"}]}, config)
    checkpoint = saver.get_tuple(config)

    assert checkpoint is not None
    assert checkpoint_metadata_uses_delta(checkpoint.metadata)
    assert not isinstance(
        checkpoint.checkpoint.get("channel_values", {}).get("messages"), list
    )


def test_snapshot_frequency_is_compiled_into_delta_channel() -> None:
    graph = _graph("delta", _memory_saver(), frequency=7)
    assert graph.channels["messages"].snapshot_frequency == 7


def test_checkpoint_mode_and_frequency_cannot_hot_switch() -> None:
    assert freeze_checkpoint_channel_mode("full") == "full"
    assert freeze_checkpoint_snapshot_frequency(10) == 10
    with pytest.raises(CheckpointModeReconfigurationError):
        freeze_checkpoint_channel_mode("delta")
    with pytest.raises(CheckpointModeReconfigurationError):
        freeze_checkpoint_snapshot_frequency(11)


@pytest.mark.asyncio
async def test_sync_and_async_checkpoint_calls_share_one_thread_lock() -> None:
    from deerflow.runtime.checkpoint_lock import (
        checkpoint_thread_lock_async,
        checkpoint_thread_lock_sync,
    )

    sync_entered = threading.Event()
    release_sync = threading.Event()
    async_entered = asyncio.Event()

    def sync_holder() -> None:
        with checkpoint_thread_lock_sync("shared-lock"):
            sync_entered.set()
            assert release_sync.wait(2)

    holder = threading.Thread(target=sync_holder)
    holder.start()
    assert await asyncio.to_thread(sync_entered.wait, 1)

    async def async_waiter() -> None:
        async with checkpoint_thread_lock_async("shared-lock"):
            async_entered.set()

    waiter = asyncio.create_task(async_waiter())
    await asyncio.sleep(0.05)
    assert not async_entered.is_set()
    release_sync.set()
    await asyncio.wait_for(waiter, 1)
    holder.join(1)
    assert async_entered.is_set()
    assert not holder.is_alive()


@pytest.mark.asyncio
async def test_cancelled_async_lock_waiter_does_not_wedge_thread() -> None:
    from deerflow.runtime.checkpoint_lock import (
        checkpoint_thread_lock_async,
        checkpoint_thread_lock_sync,
    )

    sync_entered = threading.Event()
    release_sync = threading.Event()

    def sync_holder() -> None:
        with checkpoint_thread_lock_sync("cancel-lock"):
            sync_entered.set()
            assert release_sync.wait(2)

    holder = threading.Thread(target=sync_holder)
    holder.start()
    assert await asyncio.to_thread(sync_entered.wait, 1)

    async def blocked_waiter() -> None:
        async with checkpoint_thread_lock_async("cancel-lock"):
            raise AssertionError("cancelled waiter must not enter the critical section")

    waiter = asyncio.create_task(blocked_waiter())
    await asyncio.sleep(0.05)
    waiter.cancel()
    release_sync.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    holder.join(1)

    async with asyncio.timeout(1):
        async with checkpoint_thread_lock_async("cancel-lock"):
            pass


def test_full_mode_rejects_existing_delta_thread() -> None:
    saver = _memory_saver()
    graph = _graph("delta", saver)
    config: dict[str, Any] = {"configurable": {"thread_id": "mixed-mode"}}
    inject_checkpoint_mode(config, "delta")
    graph.invoke({"messages": [{"role": "user", "content": "one"}]}, config)

    with pytest.raises(CheckpointModeMismatchError):
        ensure_checkpoint_mode_compatible(saver, config, "full")


def test_cached_saver_hits_and_thread_purge() -> None:
    cached_saver_module = importlib.reload(
        importlib.import_module("deerflow.runtime.checkpointer.cached_saver")
    )
    inner = _memory_saver()
    cache = MemoryCheckpointHistoryCache(max_entries=32)
    saver = cached_saver_module.CachedHistorySaver(
        inner, cache, key_prefix="test-db"
    )
    graph, _ = _run_two_turns("delta", saver)
    config = {"configurable": {"thread_id": "thread-delta"}}

    graph.get_state(config)
    first = saver.stats()
    graph.get_state(config)
    second = saver.stats()

    assert second["hits"] > first["hits"]

    key = make_history_key("test-db", "thread-delta", "", "checkpoint", "messages")
    cache.set_many({key: {"writes": ["secret"]}})
    saver.delete_thread("thread-delta")
    assert cache.stats().entries == 0


def test_cache_purge_does_not_match_thread_id_prefixes() -> None:
    cache = MemoryCheckpointHistoryCache(max_entries=8)
    short_key = make_history_key("db", "thread", "", "cp-1", "messages")
    prefixed_key = make_history_key(
        "db", "thread:child", "", "cp-1", "messages"
    )
    cache.set_many(
        {
            short_key: {"writes": ["short"]},
            prefixed_key: {"writes": ["child"]},
        }
    )

    cache.delete_thread("db", "thread")

    assert cache.get_many([short_key]) == {}
    assert cache.get_many([prefixed_key]) == {
        prefixed_key: {"writes": ["child"]}
    }


@pytest.mark.asyncio
async def test_redis_cache_read_failure_is_fail_open(monkeypatch) -> None:
    from redis.exceptions import RedisError

    from deerflow.runtime.checkpoint_cache.redis import RedisCheckpointHistoryCache

    class BrokenClient:
        async def mget(self, _keys):
            raise RedisError("offline")

    cache = object.__new__(RedisCheckpointHistoryCache)
    cache._client = BrokenClient()
    cache._hits = 0
    cache._misses = 0
    cache._serde = None
    cache._ttl = None

    assert await cache.aget_many(["a", "b"]) == {}
    assert cache.stats().misses == 2


@pytest.mark.asyncio
async def test_delta_rollback_restores_materialized_state() -> None:
    checkpoint_state = importlib.reload(
        importlib.import_module("deerflow.runtime.checkpoint_state")
    )
    worker = importlib.reload(
        importlib.import_module("deerflow.runtime.runs.worker")
    )
    saver = _memory_saver()
    graph = _graph("delta", saver)
    config: dict[str, Any] = {
        "configurable": {"thread_id": "rollback-delta", "checkpoint_ns": ""}
    }
    inject_checkpoint_mode(config, "delta")
    graph.invoke({"messages": [{"role": "user", "content": "one"}]}, config)
    accessor = checkpoint_state.CheckpointStateAccessor.bind(
        graph, saver, mode="delta"
    )
    point = await worker._capture_rollback_point(accessor, saver, config)
    assert point is not None
    before = [message.content for message in point.messages]

    graph.invoke({"messages": [{"role": "user", "content": "two"}]}, config)
    await worker._rollback_to_pre_run_checkpoint(
        accessor=accessor,
        checkpointer=saver,
        thread_id="rollback-delta",
        run_id="run-1",
        rollback_point=point,
        snapshot_capture_failed=False,
    )

    restored = await accessor.aget(config)
    assert [message.content for message in restored.values["messages"]] == before


@pytest.mark.asyncio
async def test_api_sync_and_async_checkpointers_are_cached_in_delta_mode() -> None:
    from app.config import settings
    from app.dependencies import ClientManager

    original_type = settings.checkpointer_type
    original_mode = settings.checkpoint_channel_mode
    manager = ClientManager()
    try:
        settings.checkpointer_type = "memory"
        settings.checkpoint_channel_mode = "delta"

        assert type(manager._get_checkpointer()).__name__ == "CachedHistorySaver"
        assert type(await manager._get_async_checkpointer()).__name__ == (
            "CachedHistorySaver"
        )
    finally:
        settings.checkpointer_type = original_type
        settings.checkpoint_channel_mode = original_mode
        if manager._async_checkpoint_cache_cm is not None:
            await manager._async_checkpoint_cache_cm.__aexit__(None, None, None)
