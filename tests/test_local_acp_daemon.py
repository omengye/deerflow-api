from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from deerflow.acp.config import LocalACPConfig
from deerflow.acp import daemon_endpoint
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


async def manage(endpoint: DaemonEndpoint, request: Any) -> dict[str, Any]:
    reader, writer, response = await connect(endpoint, "MANAGE")
    assert response == "OK"
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    payload = json.loads(await reader.readline())
    assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()
    return payload


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


def test_runtime_dir_uses_portable_product_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEER_FLOW_ACP_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(daemon_endpoint, "_portable_root", lambda: tmp_path)
    assert get_runtime_dir() == (tmp_path / "user-data" / "runtime" / "acp").resolve()


@pytest.mark.asyncio
async def test_daemon_accepts_multiple_clients_status_stop_and_reconnect(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    store = LocalACPSessionStore(config.session_store_path)
    store.setup()
    daemon = ACPDaemon(
        config, store, FakeRuntime(), tmp_path / "runtime", token="test-token"
    )  # type: ignore[arg-type]
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

    second_reader, second_writer, second = await connect(endpoint, "ACP")
    assert second == "OK"
    second_writer.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "test-2", "version": "1"},
                },
            }
        ).encode()
        + b"\n"
    )
    await second_writer.drain()
    second_initialized = json.loads(await second_reader.readline())
    assert second_initialized["result"]["protocolVersion"] == 1

    count_reader, count_writer, count_status = await connect(endpoint, "STATUS")
    assert "connections=2" in count_status
    assert await count_reader.read() == b""
    count_writer.close()
    await count_writer.wait_closed()

    first_writer.close()
    await first_writer.wait_closed()
    for _ in range(100):
        if len(daemon._connections) == 1:
            break
        await asyncio.sleep(0.01)
    assert len(daemon._connections) == 1

    reconnect_reader, reconnect_writer, reconnect = await connect(endpoint, "ACP")
    assert reconnect == "OK"
    reconnect_writer.close()
    await reconnect_writer.wait_closed()
    await reconnect_reader.read()

    second_writer.close()
    await second_writer.wait_closed()
    await second_reader.read()

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
async def test_daemon_enforces_connection_capacity_without_blocking_control_commands(
    tmp_path: Path,
) -> None:
    config = LocalACPConfig(
        config_path=tmp_path / "config.yaml",
        checkpointer_path=tmp_path / "checkpoints.db",
        session_store_path=tmp_path / "sessions.db",
        max_active_connections=1,
    )
    store = LocalACPSessionStore(config.session_store_path)
    store.setup()
    daemon = ACPDaemon(
        config, store, FakeRuntime(), tmp_path / "runtime", token="test-token"
    )  # type: ignore[arg-type]
    endpoint = await daemon.start()

    first_reader, first_writer, first = await connect(endpoint, "ACP")
    assert first == "OK"

    busy_reader, busy_writer, busy = await connect(endpoint, "ACP")
    assert busy == "BUSY"
    assert await busy_reader.read() == b""
    busy_writer.close()
    await busy_writer.wait_closed()

    status_reader, status_writer, status = await connect(endpoint, "STATUS")
    assert "connections=1" in status
    assert await status_reader.read() == b""
    status_writer.close()
    await status_writer.wait_closed()

    first_writer.close()
    await first_writer.wait_closed()
    await first_reader.read()
    await daemon.close()
    store.close()


@pytest.mark.asyncio
async def test_daemon_rejects_bad_token(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = LocalACPSessionStore(config.session_store_path)
    store.setup()
    daemon = ACPDaemon(
        config, store, FakeRuntime(), tmp_path / "runtime", token="right"
    )  # type: ignore[arg-type]
    endpoint = await daemon.start()
    reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
    writer.write(b"DFACP/1 wrong STATUS\n")
    await writer.drain()
    assert await reader.readline() == b"UNAUTHORIZED\n"
    writer.close()
    await writer.wait_closed()
    await daemon.close()
    store.close()


@pytest.mark.asyncio
async def test_daemon_management_json_roundtrip_and_invalid_request(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    async def handler(request: dict[str, Any]) -> dict[str, Any]:
        calls.append(request)
        return {"ok": True, "data": {"echo": request}}

    config = make_config(tmp_path)
    store = LocalACPSessionStore(config.session_store_path)
    store.setup()
    daemon = ACPDaemon(
        config,
        store,
        FakeRuntime(),
        tmp_path / "runtime",
        token="test-token",
        management_handler=handler,
    )  # type: ignore[arg-type]
    endpoint = await daemon.start()

    request = {"operation": "proposal.list", "status": "pending_review"}
    assert await manage(endpoint, request) == {
        "ok": True,
        "data": {"echo": request},
    }
    assert calls == [request]

    reader, writer, response = await connect(endpoint, "MANAGE")
    assert response == "OK"
    writer.write(b"not-json\n")
    await writer.drain()
    invalid = json.loads(await reader.readline())
    assert invalid["ok"] is False
    assert invalid["code"] == "invalid_request"
    writer.close()
    await writer.wait_closed()

    await daemon.close()
    store.close()


@pytest.mark.asyncio
async def test_daemon_close_aborts_an_active_acp_bridge(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = LocalACPSessionStore(config.session_store_path)
    store.setup()
    daemon = ACPDaemon(
        config, store, FakeRuntime(), tmp_path / "runtime", token="test-token"
    )  # type: ignore[arg-type]
    endpoint = await daemon.start()

    reader, writer, response = await connect(endpoint, "ACP")
    assert response == "OK"

    await asyncio.wait_for(daemon.close(), timeout=1)

    try:
        assert await asyncio.wait_for(reader.read(), timeout=1) == b""
    except ConnectionResetError:
        pass
    writer.close()
    store.close()
    assert daemon._connections == {}
    assert daemon._handlers == {}
    assert not daemon.endpoint_path.exists()
