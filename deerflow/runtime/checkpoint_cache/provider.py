"""Factories for checkpoint history cache lifetimes."""

from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

from deerflow.config.checkpointer_config import CheckpointCacheConfig

from .base import CACHE_FORMAT_VERSION
from .memory import MemoryCheckpointHistoryCache


def checkpoint_cache_key_prefix(
    config: CheckpointCacheConfig,
    database_identity: str,
) -> str:
    if config.key_prefix:
        return config.key_prefix
    digest = hashlib.sha256(database_identity.encode()).hexdigest()[:12]
    return f"ckpt-hist:v{CACHE_FORMAT_VERSION}:{digest}"


@contextlib.contextmanager
def make_sync_checkpoint_cache(
    config: CheckpointCacheConfig,
) -> Iterator[MemoryCheckpointHistoryCache]:
    if config.type == "redis":
        raise ValueError(
            "Redis checkpoint cache is async-only; use the API async checkpointer path"
        )
    cache = MemoryCheckpointHistoryCache(max_entries=config.max_entries)
    yield cache


@contextlib.asynccontextmanager
async def make_async_checkpoint_cache(
    config: CheckpointCacheConfig,
    *,
    serde: Any,
) -> AsyncIterator[Any]:
    cache: Any
    if config.type == "memory" or config.max_entries == 0:
        cache = MemoryCheckpointHistoryCache(max_entries=config.max_entries)
    else:
        from .redis import RedisCheckpointHistoryCache

        redis_url = (
            config.redis_url
            or os.getenv("DEER_FLOW_CHECKPOINT_CACHE_REDIS_URL")
            or os.getenv("REDIS_URL")
            or "redis://localhost:6379/0"
        )
        cache = RedisCheckpointHistoryCache(
            redis_url,
            serde=serde,
            ttl_seconds=config.ttl_seconds,
        )
    try:
        yield cache
    finally:
        await cache.aclose()
