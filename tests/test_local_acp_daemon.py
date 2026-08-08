from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from deerflow.acp.config import LocalACPConfig
from deerflow.acp.daemon import ACPDaemon
from deerflow.acp.daemon_endpoint import (
    DaemonAlreadyRunning,
    DaemonEndpoint,
    SingleInstanceLock,
    get_runtime_dir,
)
from deerflow.acp.session_store import LocalACPSessionStore


class FakeRuntime:
    async def astream(self, *args: Any, **kwargs: Any):
        del args, kwargs
        if False:
            yield None

    async def history(self, session_id: str) -> list[dict[str, Any]]:
        del session_id
        return []


def make_config(tmp_path: Path) -> LocalACPConfig:
    return LocalACPConfig(
        config_path=tmp_path / "config.yaml",
        checkpointer_path=tmp_path / "checkpoints.db",
        session_store_path=tmp_path / "sessions.db",
    )


async def connect(
    endpoint: DaemonEndpoint, command: str
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
    reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
    writer.write(f"DFACP/1 {endpoint.token} {command}\n".encode())
    await writer.drain()
    response = (await reader.readline()).decode().strip()
    return reader, writer, response


def test_endpoint_roundtrip_runtime_override_and_single_instance_lock(
    tmp_path: Path,
) -> None:
    runtime_dir = get_runtime_dir(tmp_path / "runtime")
    assert runtime_dir == (tmp_path / "runtime").resolve()

    endpoint_path = runtime_dir / "endpoint.json"
    endpoint = DaemonEndpoint(
        host="127.0.0.1",
        port=1234,
        token="secret",
        pid=42,
        build_id="test",
        config_path=str(tmp_path / "config.yaml"),
    )
    endpoint.publish(endpoint_path)
    assert DaemonEndpoint.load(endpoint_path) == endpoint

    first = SingleInstanceLock(runtime_dir / "daemon.lock")
    second = SingleInstanceLock(runtime_dir / "daemon.lock")
    first.acquire()
    try:
        with pytest.raises(DaemonAlreadyRunning):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


@pytest.mark.asyncio
async def test_daemon_initialize_busy_status_stop_and_reconnect(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = LocalACPSessionStore(config.session_store_path)
    store.setup()
    daemon = ACPDaemon(config, store, FakeRuntime(), tmp_path / "runtime", token="test-token")  # type: ignore[arg-type]
    endpoint = await daemon.start()
    assert DaemonEndpoint.load(daemon.endpoint_path) == endpoint

    status_reader, status_writer, status = await connect(endpoint, "STATUS")
    assert status.startswith("OK ")
    assert await status_reader.read() == b""
    status_writer.close()
    await status_writer.wait_closed()

    first_reader, first_writer, response = await connect(endpoint, "ACP")
    assert response == "OK"
    first_writer.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        ).encode()
        + b"\n"
    )
    await first_writer.drain()
    initialized = json.loads(await first_reader.readline())
    assert initialized["result"]["protocolVersion"] == 1

    busy_reader, busy_writer, busy = await connect(endpoint, "ACP")
    assert busy == "BUSY"
    assert await busy_reader.read() == b""
    busy_writer.close()
    await busy_writer.wait_closed()

    first_writer.close()
    await first_writer.wait_closed()
    for _ in range(100):
        if not daemon._active:
            break
        await asyncio.sleep(0.01)
    assert daemon._active is False

    reconnect_reader, reconnect_writer, reconnect = await connect(endpoint, "ACP")
    assert reconnect == "OK"
    reconnect_writer.close()
    await reconnect_writer.wait_closed()
    await reconnect_reader.read()

    stop_reader, stop_writer, stopped = await connect(endpoint, "STOP")
    assert stopped == "OK"
    assert await stop_reader.read() == b""
    stop_writer.close()
    await stop_writer.wait_closed()
    await asyncio.wait_for(daemon.stop_requested.wait(), timeout=1)

    await daemon.close()
    store.close()
    assert not daemon.endpoint_path.exists()


@pytest.mark.asyncio
async def test_daemon_rejects_bad_token(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = LocalACPSessionStore(config.session_store_path)
    store.setup()
    daemon = ACPDaemon(config, store, FakeRuntime(), tmp_path / "runtime", token="right")  # type: ignore[arg-type]
    endpoint = await daemon.start()
    reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
    writer.write(b"DFACP/1 wrong STATUS\n")
    await writer.drain()
    assert await reader.readline() == b"UNAUTHORIZED\n"
    writer.close()
    await writer.wait_closed()
    await daemon.close()
    store.close()
