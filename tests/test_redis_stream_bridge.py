import asyncio
import unittest
from typing import Any

from deerflow.runtime import END_SENTINEL, HEARTBEAT_SENTINEL
from deerflow.runtime.stream_bridge.redis import RedisStreamBridge


def _redis_id_gt(left: str, right: str) -> bool:
    left_ms, left_seq = (int(part) for part in left.split("-", 1))
    right_ms, right_seq = (int(part) for part in right.split("-", 1))
    return (left_ms, left_seq) > (right_ms, right_seq)


class _FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.deleted: list[str] = []
        self.expires: dict[str, int] = {}

    async def ping(self) -> bool:
        return True

    async def xadd(
        self,
        name: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> str:
        stream = self.streams.setdefault(name, [])
        entry_id = f"1-{len(stream)}"
        stream.append((entry_id, fields))
        if maxlen is not None and len(stream) > maxlen:
            del stream[: len(stream) - maxlen]
        return entry_id

    async def expire(self, name: str, seconds: int) -> bool:
        self.expires[name] = seconds
        return True

    async def xread(
        self,
        streams: dict[str, str],
        *,
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        name, last_id = next(iter(streams.items()))
        entries = [entry for entry in self.streams.get(name, []) if _redis_id_gt(entry[0], last_id)]
        if count is not None:
            entries = entries[:count]
        if not entries:
            await asyncio.sleep(0)
            return []
        return [(name, entries)]

    async def delete(self, name: str) -> int:
        self.deleted.append(name)
        self.streams.pop(name, None)
        return 1

    async def aclose(self) -> None:
        return None


async def _next(iterator: Any):
    return await anext(iterator)


class RedisStreamBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_replays_events_and_end_from_redis_stream(self) -> None:
        fake = _FakeRedis()
        bridge = RedisStreamBridge(client=fake, key_prefix="test", retention_seconds=60)

        await bridge.publish("run-1", "metadata", {"run_id": "run-1"})
        await bridge.publish("run-1", "messages-tuple", {"content": "hello"})
        await bridge.publish_end("run-1")

        sub = bridge.subscribe("run-1", heartbeat_interval=0.001)
        first = await _next(sub)
        second = await _next(sub)
        end = await _next(sub)

        self.assertEqual(first.id, "1-0")
        self.assertEqual(first.event, "metadata")
        self.assertEqual(first.data, {"run_id": "run-1"})
        self.assertEqual(second.id, "1-1")
        self.assertEqual(second.event, "messages-tuple")
        self.assertEqual(second.data, {"content": "hello"})
        self.assertIs(end, END_SENTINEL)
        self.assertEqual(fake.expires["test:run-1"], 60)

    async def test_last_event_id_resumes_after_seen_entry(self) -> None:
        fake = _FakeRedis()
        bridge = RedisStreamBridge(client=fake, key_prefix="test", retention_seconds=0)

        await bridge.publish("run-1", "metadata", {"run_id": "run-1"})
        await bridge.publish("run-1", "messages-tuple", {"content": "hello"})

        sub = bridge.subscribe("run-1", last_event_id="1-0", heartbeat_interval=0.001)
        resumed = await _next(sub)

        self.assertEqual(resumed.id, "1-1")
        self.assertEqual(resumed.event, "messages-tuple")
        self.assertEqual(resumed.data, {"content": "hello"})

    async def test_empty_read_emits_heartbeat(self) -> None:
        bridge = RedisStreamBridge(client=_FakeRedis(), key_prefix="test", retention_seconds=0)

        sub = bridge.subscribe("missing", heartbeat_interval=0.001)
        event = await _next(sub)

        self.assertIs(event, HEARTBEAT_SENTINEL)

    async def test_owned_redis_client_disables_socket_read_timeout(self) -> None:
        bridge = RedisStreamBridge(redis_url="redis://127.0.0.1:6379/1")
        try:
            kwargs = bridge._redis.connection_pool.connection_kwargs
            self.assertIsNone(kwargs["socket_timeout"])
        finally:
            await bridge.close()

    async def test_cleanup_deletes_key_after_retention_window(self) -> None:
        fake = _FakeRedis()
        bridge = RedisStreamBridge(client=fake, key_prefix="test", retention_seconds=0)
        await bridge.publish("run-1", "metadata", {"run_id": "run-1"})

        await bridge.cleanup("run-1", delay=0)

        self.assertEqual(fake.deleted, ["test:run-1"])
        self.assertNotIn("test:run-1", fake.streams)

    async def test_expire_discards_buffer_and_drops_future_events(self) -> None:
        fake = _FakeRedis()
        bridge = RedisStreamBridge(client=fake, key_prefix="test", retention_seconds=60)
        await bridge.publish("run-1", "metadata", {"run_id": "run-1"})

        await bridge.expire("run-1")
        await bridge.publish("run-1", "messages-tuple", {"content": "late"})

        self.assertNotIn("test:run-1", fake.streams)
        sub = bridge.subscribe("run-1", heartbeat_interval=0.001)
        self.assertIs(await _next(sub), END_SENTINEL)
