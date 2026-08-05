"""Read-through cache for immutable LangGraph delta-channel histories."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple

from deerflow.runtime.checkpoint_cache.base import make_history_key

_COMPOSE_MAX_DEPTH = 8


def _checkpoint_ref(tup: CheckpointTuple) -> tuple[str, str, str]:
    configurable = tup.config["configurable"]
    return (
        str(configurable["thread_id"]),
        str(configurable.get("checkpoint_ns", "")),
        str(configurable["checkpoint_id"]),
    )


def _channel_writes(tup: CheckpointTuple, channel: str) -> list[Any]:
    return [write for write in (tup.pending_writes or []) if write[1] == channel]


class CachedHistorySaver(BaseCheckpointSaver):
    """Delegate saver operations while caching only resolved delta histories."""

    def __init__(self, inner: BaseCheckpointSaver, cache: Any, *, key_prefix: str) -> None:
        self.serde = inner.serde
        self._inner = inner
        self._cache = cache
        self._key_prefix = key_prefix
        self._compose_hits = 0
        self._full_walks = 0

    @property
    def inner(self) -> BaseCheckpointSaver:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    def _key(self, tup: CheckpointTuple, channel: str) -> str:
        thread_id, namespace, checkpoint_id = _checkpoint_ref(tup)
        return make_history_key(
            self._key_prefix, thread_id, namespace, checkpoint_id, channel
        )

    def stats(self) -> dict[str, int]:
        return {
            **self._cache.stats().as_dict(),
            "compose_hits": self._compose_hits,
            "full_walks": self._full_walks,
        }

    async def aget_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> dict[str, Any]:
        if not channels:
            return {}
        if not getattr(self._cache, "enabled", True):
            return await self._awalk(config, channels)
        target = await self._inner.aget_tuple(config)
        if target is None:
            return await self._awalk(config, channels)
        keys = {channel: self._key(target, channel) for channel in channels}
        hits = await self._cache.aget_many(list(keys.values()))
        missing = [channel for channel in channels if keys[channel] not in hits]
        computed = {
            channel: await self._aresolve(target, channel, _COMPOSE_MAX_DEPTH)
            for channel in missing
        }
        if computed:
            await self._cache.aset_many(
                {keys[channel]: value for channel, value in computed.items()}
            )
        return {
            channel: hits.get(keys[channel])
            or computed.get(channel)
            or {"writes": []}
            for channel in channels
        }

    async def _aresolve(
        self, tup: CheckpointTuple, channel: str, depth: int
    ) -> dict[str, Any]:
        if tup.parent_config is None:
            return {"writes": []}
        parent = await self._inner.aget_tuple(tup.parent_config)
        if parent is None:
            return {"writes": []}
        writes = _channel_writes(parent, channel)
        channel_values = parent.checkpoint.get("channel_values") or {}
        if channel in channel_values:
            self._compose_hits += 1
            return {"writes": writes, "seed": channel_values[channel]}
        key = self._key(parent, channel)
        history = (await self._cache.aget_many([key])).get(key)
        if history is None:
            if depth > 0:
                history = await self._aresolve(parent, channel, depth - 1)
            else:
                self._full_walks += 1
                walked = await self._inner.aget_delta_channel_history(
                    config=parent.config, channels=[channel]
                )
                history = walked.get(channel) or {"writes": []}
            await self._cache.aset_many({key: history})
        self._compose_hits += 1
        result: dict[str, Any] = {
            "writes": list(history.get("writes", [])) + writes
        }
        if "seed" in history:
            result["seed"] = history["seed"]
        return result

    async def _awalk(
        self, config: RunnableConfig, channels: Sequence[str]
    ) -> dict[str, Any]:
        self._full_walks += 1
        return dict(
            await self._inner.aget_delta_channel_history(
                config=config, channels=channels
            )
        )

    def get_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> dict[str, Any]:
        if not channels:
            return {}
        if not getattr(self._cache, "enabled", True):
            return self._walk(config, channels)
        if not hasattr(self._cache, "get_many"):
            raise TypeError("sync delta history requires a memory cache backend")
        target = self._inner.get_tuple(config)
        if target is None:
            return self._walk(config, channels)
        keys = {channel: self._key(target, channel) for channel in channels}
        hits = self._cache.get_many(list(keys.values()))
        missing = [channel for channel in channels if keys[channel] not in hits]
        computed = {
            channel: self._resolve(target, channel, _COMPOSE_MAX_DEPTH)
            for channel in missing
        }
        if computed:
            self._cache.set_many(
                {keys[channel]: value for channel, value in computed.items()}
            )
        return {
            channel: hits.get(keys[channel])
            or computed.get(channel)
            or {"writes": []}
            for channel in channels
        }

    def _resolve(
        self, tup: CheckpointTuple, channel: str, depth: int
    ) -> dict[str, Any]:
        if tup.parent_config is None:
            return {"writes": []}
        parent = self._inner.get_tuple(tup.parent_config)
        if parent is None:
            return {"writes": []}
        writes = _channel_writes(parent, channel)
        channel_values = parent.checkpoint.get("channel_values") or {}
        if channel in channel_values:
            self._compose_hits += 1
            return {"writes": writes, "seed": channel_values[channel]}
        key = self._key(parent, channel)
        history = self._cache.get_many([key]).get(key)
        if history is None:
            if depth > 0:
                history = self._resolve(parent, channel, depth - 1)
            else:
                self._full_walks += 1
                history = self._inner.get_delta_channel_history(
                    config=parent.config, channels=[channel]
                ).get(channel) or {"writes": []}
            self._cache.set_many({key: history})
        self._compose_hits += 1
        result: dict[str, Any] = {
            "writes": list(history.get("writes", [])) + writes
        }
        if "seed" in history:
            result["seed"] = history["seed"]
        return result

    def _walk(
        self, config: RunnableConfig, channels: Sequence[str]
    ) -> dict[str, Any]:
        self._full_walks += 1
        return dict(
            self._inner.get_delta_channel_history(
                config=config, channels=channels
            )
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self._inner.get_tuple(config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        return self._inner.list(config, filter=filter, before=before, limit=limit)

    def put(self, config, checkpoint, metadata, new_versions):
        return self._inner.put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path="") -> None:
        self._inner.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        self._inner.delete_thread(thread_id)
        self._cache.delete_thread(self._key_prefix, thread_id)

    def delete_for_runs(self, run_ids: Sequence[str]) -> None:
        self._inner.delete_for_runs(run_ids)

    def copy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        self._inner.copy_thread(source_thread_id, target_thread_id)

    def prune(
        self, thread_ids: Sequence[str], *, strategy: str = "keep_latest"
    ) -> None:
        self._inner.prune(thread_ids, strategy=strategy)
        for thread_id in thread_ids:
            self._cache.delete_thread(self._key_prefix, thread_id)

    async def aget_tuple(self, config):
        return await self._inner.aget_tuple(config)

    def alist(self, config, *, filter=None, before=None, limit=None):
        return self._inner.alist(config, filter=filter, before=before, limit=limit)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await self._inner.aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path="") -> None:
        await self._inner.aput_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await self._inner.adelete_thread(thread_id)
        await self._cache.adelete_thread(self._key_prefix, thread_id)

    async def adelete_for_runs(self, run_ids: Sequence[str]) -> None:
        await self._inner.adelete_for_runs(run_ids)

    async def acopy_thread(
        self, source_thread_id: str, target_thread_id: str
    ) -> None:
        await self._inner.acopy_thread(source_thread_id, target_thread_id)

    async def aprune(
        self, thread_ids: Sequence[str], *, strategy: str = "keep_latest"
    ) -> None:
        await self._inner.aprune(thread_ids, strategy=strategy)
        for thread_id in thread_ids:
            await self._cache.adelete_thread(self._key_prefix, thread_id)

    def get_next_version(self, current: Any, channel: Any) -> Any:
        return self._inner.get_next_version(current, channel)
