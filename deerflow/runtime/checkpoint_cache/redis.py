"""Shared Redis history cache; outages degrade to cache misses."""

from __future__ import annotations

import logging
from typing import Any

from .base import CheckpointCacheStats, redis_glob_escape, thread_key_stem

logger = logging.getLogger(__name__)
_TAG_SEPARATOR = b"\x00"


class RedisCheckpointHistoryCache:
    def __init__(
        self,
        redis_url: str,
        *,
        serde: Any,
        ttl_seconds: int = 86400,
    ) -> None:
        import redis.asyncio as redis_async

        self._client = redis_async.from_url(redis_url, decode_responses=False)
        self._serde = serde
        self._ttl = ttl_seconds if ttl_seconds > 0 else None
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _redis_error() -> type[Exception]:
        from redis.exceptions import RedisError

        return RedisError

    async def aget_many(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        if not keys:
            return {}
        try:
            values = await self._client.mget(keys)
        except self._redis_error() as exc:
            logger.warning(
                "checkpoint cache read failed; treating as all-miss: %s", exc
            )
            self._misses += len(keys)
            return {}
        found: dict[str, dict[str, Any]] = {}
        for key, raw in zip(keys, values, strict=True):
            if raw is None:
                self._misses += 1
                continue
            try:
                if isinstance(raw, str):
                    raw = raw.encode()
                tag, payload = raw.split(_TAG_SEPARATOR, 1)
                found[key] = self._serde.loads_typed((tag.decode(), payload))
                self._hits += 1
            except Exception:
                self._misses += 1
                logger.warning("checkpoint cache entry decode failed", exc_info=True)
        return found

    async def aset_many(self, entries: dict[str, dict[str, Any]]) -> None:
        if not entries:
            return
        try:
            pipe = self._client.pipeline(transaction=False)
            for key, entry in entries.items():
                tag, payload = self._serde.dumps_typed(entry)
                pipe.set(key, tag.encode() + _TAG_SEPARATOR + payload, ex=self._ttl)
            await pipe.execute()
        except self._redis_error() as exc:
            logger.warning("checkpoint cache write failed; skipping: %s", exc)

    async def adelete_thread(self, key_prefix: str, thread_id: str) -> None:
        stem = thread_key_stem(key_prefix, thread_id)
        try:
            cursor = 0
            pattern = redis_glob_escape(stem) + "*"
            while True:
                cursor, keys = await self._client.scan(
                    cursor=cursor, match=pattern, count=500
                )
                if keys:
                    await self._client.unlink(*keys)
                if cursor == 0:
                    break
        except self._redis_error() as exc:
            logger.warning(
                "checkpoint cache thread purge failed; TTL bounds retention: %s",
                exc,
            )

    def stats(self) -> CheckpointCacheStats:
        return CheckpointCacheStats(hits=self._hits, misses=self._misses)

    async def aclose(self) -> None:
        await self._client.aclose()
