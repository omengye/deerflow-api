from __future__ import annotations

import asyncio
import json

from raft_deerflow_adapter.activity import ActivityQueue
from raft_deerflow_adapter.wake_server import WakeServer


async def _request(port: int, request: bytes) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    status_line = await reader.readline()
    status = int(status_line.split()[1])
    headers = {}
    while line := await reader.readline():
        if line == b"\r\n":
            break
        name, value = line.decode().split(":", 1)
        headers[name.lower()] = value.strip()
    body = await reader.readexactly(int(headers["content-length"]))
    writer.close()
    await writer.wait_closed()
    return status, json.loads(body)


async def test_wake_endpoint_accepts_current_raft_schema() -> None:
    received = []

    async def callback(payload):
        received.append(payload)

    server = WakeServer("127.0.0.1", 0, "token", "runtime-1", callback)
    await server.start()
    try:
        body = json.dumps(
            {"schema": "raft-channel-wake.v1", "eventId": "event-1"}
        ).encode()
        request = (
            b"POST /wake HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"x-raft-bridge-token: token\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        status, response = await _request(server.port, request)
        assert status == 200
        assert response == {"ok": True, "runtimeSession": "runtime-1"}
        assert received[0]["eventId"] == "event-1"

        status, response = await _request(
            server.port,
            b"GET /activity/drain?max=50 HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"x-raft-bridge-token: token\r\n\r\n",
        )
        assert status == 200
        assert response["schema"] == "raft-activity-drain.v1"
        assert response["events"] == []
    finally:
        await server.close()


async def test_activity_endpoint_drains_oldest_events_and_reports_drops() -> None:
    async def callback(_payload):
        return None

    activity = ActivityQueue(capacity=2)
    server = WakeServer(
        "127.0.0.1", 0, "token", "runtime-1", callback, activity=activity
    )
    await server.start()
    try:
        activity.emit("UserPromptSubmit", session_id="session-1")
        activity.emit("ThinkingStart", session_id="session-1")
        activity.emit("PreToolUse", session_id="session-1", tool_name="search")

        status, response = await _request(
            server.port,
            b"GET /activity/drain?max=1 HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"x-raft-bridge-token: token\r\n\r\n",
        )
        assert status == 200
        assert response["dropped"] == 1
        assert [event["hookEventName"] for event in response["events"]] == [
            "ThinkingStart"
        ]
        assert "toolInput" not in response["events"][0]
        assert "toolOutput" not in response["events"][0]

        _status, response = await _request(
            server.port,
            b"GET /activity/drain?max=bad HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"x-raft-bridge-token: token\r\n\r\n",
        )
        assert response["dropped"] == 0
        assert [event["hookEventName"] for event in response["events"]] == [
            "PreToolUse"
        ]
    finally:
        await server.close()
