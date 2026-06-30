"""Redis Streams backed stream bridge."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from .base import END_SENTINEL, HEARTBEAT_SENTINEL, StreamBridge, StreamEvent

logger = logging.getLogger(__name__)

_END_EVENT = "__end__"


class RedisStreamBridge(StreamBridge):
    """Per-run stream bridge implemented with Redis Streams.

    Redis entry IDs are exposed directly as ``StreamEvent.id`` so SSE clients
    can reconnect with ``Last-Event-ID`` and resume at the next Redis entry.
    """

    def __init__(
        self,
        *,
        redis_url: str = "redis://localhost:6379/0",
        key_prefix: str = "deerflow:stream",
        maxlen: int = 10000,
        retention_seconds: int = 3600,
        client: Any | None = None,
    ) -> None:
        self._key_prefix = key_prefix.rstrip(":")
        self._maxlen = maxlen
        self._retention_seconds = retention_seconds
        self._owns_client = client is None
        if client is None:
            try:
                from redis import asyncio as redis_asyncio
            except ImportError as exc:
                raise RuntimeError(
                    "Redis stream bridge requires the 'redis' package. "
                    "Install project dependencies after enabling stream_bridge.type=redis."
                ) from exc
            # XREAD uses a server-side BLOCK timeout; a client read timeout shorter
            # than that turns an idle stream into redis.exceptions.TimeoutError.
            self._redis = redis_asyncio.from_url(
                redis_url,
                decode_responses=True,
                socket_timeout=None,
            )
        else:
            self._redis = client

    def _key(self, run_id: str) -> str:
        return f"{self._key_prefix}:{run_id}"

    async def ping(self) -> None:
        ping = getattr(self._redis, "ping", None)
        if ping is not None:
            await ping()

    async def _expire(self, key: str) -> None:
        if self._retention_seconds > 0:
            await self._redis.expire(key, self._retention_seconds)

    async def _xadd(self, key: str, fields: dict[str, str]) -> str:
        return await self._redis.xadd(
            key,
            fields,
            maxlen=self._maxlen,
            approximate=True,
        )

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        key = self._key(run_id)
        await self._xadd(
            key,
            {
                "event": event,
                "data": json.dumps(data, ensure_ascii=False, default=str),
            },
        )
        await self._expire(key)

    async def publish_end(self, run_id: str) -> None:
        key = self._key(run_id)
        await self._xadd(key, {"event": _END_EVENT, "data": "{}"})
        await self._expire(key)

    async def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        key = self._key(run_id)
        redis_id = last_event_id or "0-0"
        block_ms = max(1, int(heartbeat_interval * 1000))

        while True:
            response = await self._redis.xread({key: redis_id}, count=10, block=block_ms)
            if not response:
                yield HEARTBEAT_SENTINEL
                continue

            for _stream_name, entries in response:
                for entry_id, fields in entries:
                    redis_id = entry_id
                    event = fields.get("event") if isinstance(fields, dict) else None
                    if event == _END_EVENT:
                        yield END_SENTINEL
                        return
                    if not event:
                        logger.warning("Skipping Redis stream entry without event field: key=%s id=%s", key, entry_id)
                        continue

                    raw_data = fields.get("data", "null")
                    try:
                        data = json.loads(raw_data)
                    except (TypeError, json.JSONDecodeError):
                        data = raw_data
                    yield StreamEvent(id=entry_id, event=str(event), data=data)

    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        effective_delay = max(delay, float(self._retention_seconds))
        if effective_delay > 0:
            await asyncio.sleep(effective_delay)
        await self._redis.delete(self._key(run_id))

    async def close(self) -> None:
        if not self._owns_client:
            return
        close = getattr(self._redis, "aclose", None)
        if close is None:
            close = getattr(self._redis, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
