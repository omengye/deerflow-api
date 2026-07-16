"""Async stream bridge factory.

Provides an **async context manager** aligned with
:func:`deerflow.agents.checkpointer.async_provider.make_checkpointer`.

Usage (e.g. FastAPI lifespan)::

    from deerflow.agents.stream_bridge import make_stream_bridge

    async with make_stream_bridge() as bridge:
        app.state.stream_bridge = bridge
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from deerflow.config.stream_bridge_config import get_stream_bridge_config

from .base import StreamBridge

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def make_stream_bridge(config=None) -> AsyncIterator[StreamBridge]:
    """Async context manager that yields a :class:`StreamBridge`.

    Falls back to :class:`MemoryStreamBridge` when no configuration is
    provided and nothing is set globally.
    """
    if config is None:
        config = get_stream_bridge_config()

    if config is None or config.type == "memory":
        from deerflow.runtime.stream_bridge.memory import MemoryStreamBridge

        maxsize = config.queue_maxsize if config is not None else 256
        bridge = MemoryStreamBridge(queue_maxsize=maxsize)
        logger.info("Stream bridge initialised: memory (queue_maxsize=%d)", maxsize)
        try:
            yield bridge
        finally:
            await bridge.close()
        return

    if config.type == "redis":
        from deerflow.runtime.stream_bridge.redis import RedisStreamBridge

        bridge = RedisStreamBridge(
            redis_url=config.redis_url or "redis://localhost:6379/0",
            key_prefix=config.redis_key_prefix,
            maxlen=config.redis_maxlen,
            retention_seconds=config.redis_retention_seconds,
        )
        await bridge.ping()
        logger.info(
            "Stream bridge initialised: redis (key_prefix=%s maxlen=%d safety_ttl_seconds=%d)",
            config.redis_key_prefix,
            config.redis_maxlen,
            config.redis_retention_seconds,
        )
        try:
            yield bridge
        finally:
            await bridge.close()
        return

    raise ValueError(f"Unknown stream bridge type: {config.type!r}")
