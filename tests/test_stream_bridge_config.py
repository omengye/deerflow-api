from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from deerflow.config.stream_bridge_config import StreamBridgeConfig
from deerflow.runtime.stream_bridge.base import HEARTBEAT_SENTINEL
from deerflow.runtime.stream_bridge.memory import MemoryStreamBridge


@pytest.mark.parametrize(
    "value",
    [True, False, 0, -1, float("inf"), float("nan"), 86_401],
)
def test_stream_bridge_config_rejects_invalid_heartbeat(value) -> None:
    with pytest.raises(ValidationError):
        StreamBridgeConfig(heartbeat_interval_seconds=value)


def test_stream_bridge_config_rejects_nonpositive_queue_size() -> None:
    with pytest.raises(ValidationError):
        StreamBridgeConfig(queue_maxsize=0)


async def test_memory_bridge_uses_configured_default_heartbeat() -> None:
    bridge = MemoryStreamBridge(heartbeat_interval=0.001)

    event = await asyncio.wait_for(anext(bridge.subscribe("run-1")), timeout=1)

    assert event is HEARTBEAT_SENTINEL
    assert bridge.heartbeat_interval == 0.001
