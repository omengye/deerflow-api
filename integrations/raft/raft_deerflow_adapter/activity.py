"""Loss-tolerant Raft activity telemetry for the external-agent bridge."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


ACTIVITY_EVENT_SCHEMA = "raft-activity.v1"
ACTIVITY_DRAIN_SCHEMA = "raft-activity-drain.v1"


class ActivityQueue:
    """Bounded FIFO matching Raft's external-agent activity drain contract."""

    def __init__(self, capacity: int = 500) -> None:
        self.capacity = max(1, capacity)
        self._events: deque[dict[str, Any]] = deque()
        self._dropped_since_drain = 0

    def emit(
        self,
        hook_event_name: str,
        *,
        session_id: str | None = None,
        tool_name: str | None = None,
        status: str = "ok",
        error_class: str | None = None,
    ) -> None:
        """Append one metadata-only event; never accepts prompt or tool content."""
        event: dict[str, Any] = {
            "schema": ACTIVITY_EVENT_SCHEMA,
            "eventId": str(uuid4()),
            "hookEventName": hook_event_name[:80],
            "status": status[:40],
            "occurredAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        if session_id:
            event["sessionId"] = session_id[:200]
        if tool_name:
            event["toolName"] = tool_name[:120]
        if error_class:
            event["errorClass"] = error_class[:120]

        self._events.append(event)
        while len(self._events) > self.capacity:
            self._events.popleft()
            self._dropped_since_drain += 1

    def drain(self, maximum: int = 200) -> dict[str, Any]:
        """Remove up to ``maximum`` oldest events (at-most-once telemetry)."""
        take = max(1, maximum)
        events = [self._events.popleft() for _ in range(min(take, len(self._events)))]
        dropped = self._dropped_since_drain
        self._dropped_since_drain = 0
        return {
            "schema": ACTIVITY_DRAIN_SCHEMA,
            "events": events,
            "dropped": dropped,
        }

    @property
    def size(self) -> int:
        return len(self._events)
