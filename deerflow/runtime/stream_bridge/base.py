"""Abstract stream bridge protocol.

StreamBridge decouples agent workers (producers) from SSE endpoints
(consumers), aligning with LangGraph Platform's Queue + StreamManager
architecture.
"""

from __future__ import annotations

import abc
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from deerflow.config.stream_bridge_config import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    MAX_HEARTBEAT_INTERVAL_SECONDS,
)


@dataclass(frozen=True)
class StreamEvent:
    """Single stream event.

    Attributes:
        id: Monotonically increasing event ID (used as SSE ``id:`` field,
            supports ``Last-Event-ID`` reconnection).
        event: SSE event name, e.g. ``"metadata"``, ``"updates"``,
            ``"events"``, ``"error"``, ``"end"``.
        data: JSON-serialisable payload.
    """

    id: str
    event: str
    data: Any


HEARTBEAT_SENTINEL = StreamEvent(id="", event="__heartbeat__", data=None)
END_SENTINEL = StreamEvent(id="", event="__end__", data=None)


class StreamBridge(abc.ABC):
    """Abstract base for stream bridges."""

    def __init__(
        self,
        *,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._heartbeat_interval = self._validate_heartbeat_interval(
            heartbeat_interval
        )

    @property
    def heartbeat_interval(self) -> float:
        return self._heartbeat_interval

    def _resolve_heartbeat_interval(
        self, heartbeat_interval: float | None
    ) -> float:
        if heartbeat_interval is None:
            return self._heartbeat_interval
        return self._validate_heartbeat_interval(heartbeat_interval)

    @staticmethod
    def _validate_heartbeat_interval(heartbeat_interval: float) -> float:
        if (
            isinstance(heartbeat_interval, bool)
            or not isinstance(heartbeat_interval, (int, float))
            or not math.isfinite(heartbeat_interval)
            or heartbeat_interval <= 0
            or heartbeat_interval > MAX_HEARTBEAT_INTERVAL_SECONDS
        ):
            raise ValueError(
                "heartbeat_interval must be a positive finite number no greater "
                f"than {MAX_HEARTBEAT_INTERVAL_SECONDS:g} seconds"
            )
        return float(heartbeat_interval)

    @abc.abstractmethod
    async def publish(self, run_id: str, event: str, data: Any) -> None:
        """Enqueue a single event for *run_id* (producer side)."""

    @abc.abstractmethod
    async def publish_end(self, run_id: str) -> None:
        """Signal that no more events will be produced for *run_id*."""

    @abc.abstractmethod
    async def expire(self, run_id: str) -> None:
        """Discard buffered events and stop buffering new events for *run_id*.

        The expired marker remains until :meth:`cleanup` is called so callers
        can distinguish an expired replay window from a never-created stream.
        """

    @abc.abstractmethod
    def subscribe(
        self,
        run_id: str,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Async iterator that yields events for *run_id* (consumer side).

        Yields :data:`HEARTBEAT_SENTINEL` when no event arrives within
        *heartbeat_interval* seconds.  Yields :data:`END_SENTINEL` once
        the producer calls :meth:`publish_end`.
        """

    @abc.abstractmethod
    async def cleanup(self, run_id: str, *, delay: float = 0) -> None:
        """Release resources associated with *run_id*.

        If *delay* > 0 the implementation should wait before releasing,
        giving late subscribers a chance to drain remaining events.
        """

    async def close(self) -> None:
        """Release backend resources.  Default is a no-op."""
