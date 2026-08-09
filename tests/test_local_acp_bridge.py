from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _bridge_binary(project_root: Path) -> Path:
    configured = os.getenv("DEERFLOW_ACP_BRIDGE_BIN")
    if configured:
        return Path(configured)
    suffix = ".exe" if os.name == "nt" else ""
    return project_root / "bridge" / "target" / "release" / f"deerflow-acp{suffix}"


def _wait_for(path: Path, process: subprocess.Popen[str], timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            pytest.fail(f"ACP daemon exited before becoming ready: {stderr}")
        time.sleep(0.05)
    pytest.fail(f"Timed out waiting for {path}")


def _run_bridge_session(
    bridge: Path,
    config: Path,
    runtime_dir: Path,
    cwd: Path,
) -> str:
    process = subprocess.Popen(
        [
            str(bridge),
            "--config",
            str(config),
            "--runtime-dir",
            str(runtime_dir),
            "--no-auto-start",
        ],
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.stdin is not None
    assert process.stdout is not None

    def request(request_id: int, method: str, params: dict[str, object]):
        process.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            + "\n"
        )
        process.stdin.flush()
        return json.loads(process.stdout.readline())

    initialized = request(
        1,
        "initialize",
        {
            "protocolVersion": 1,
            "clientCapabilities": {},
            "clientInfo": {"name": "bridge-test", "version": "1"},
        },
    )
    assert initialized["result"]["protocolVersion"] == 1
    created = request(
        2,
        "session/new",
        {
            "cwd": str(cwd),
            "mcpServers": [
                {
                    "name": "codeg",
                    "command": "codeg-mcp-test-not-started",
                    "args": [],
                    "env": [],
                }
            ],
        },
    )
    session_id = created["result"]["sessionId"]
    listed = request(3, "session/list", {"cwd": str(cwd)})
    assert session_id in {item["sessionId"] for item in listed["result"]["sessions"]}

    process.stdin.close()
    return_code = process.wait(timeout=20)
    stderr = process.stderr.read() if process.stderr is not None else ""
    assert return_code == 0, stderr
    return str(session_id)


def test_native_bridge_real_acp_roundtrip_and_reconnect(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    bridge = _bridge_binary(project_root)
    if not bridge.is_file():
        pytest.skip("Build bridge/Cargo.toml in release mode to run the native bridge test")

    config = tmp_path / "config.yaml"
    config.write_text(
        """
api:
  data_dir: ./data
local_acp:
  checkpointer_path: ./data/checkpoints.db
  session_store_path: ./data/sessions.db
  accept_client_mcp_servers: true
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
""".strip()
        + "\n",
        encoding="utf-8",
    )
    runtime_dir = tmp_path / "runtime"
    endpoint = runtime_dir / "endpoint.json"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(project_root), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    daemon = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "deerflow.acp.daemon",
            "--config",
            str(config),
            "--runtime-dir",
            str(runtime_dir),
            "--no-warmup",
            "--no-sandbox-warmup",
        ],
        cwd=project_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        _wait_for(endpoint, daemon)
        first_session = _run_bridge_session(bridge, config, runtime_dir, tmp_path)
        second_session = _run_bridge_session(bridge, config, runtime_dir, tmp_path)
        assert first_session != second_session

        status = subprocess.run(
            [
                str(bridge),
                "--status",
                "--config",
                str(config),
                "--runtime-dir",
                str(runtime_dir),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
        assert status.returncode == 0, status.stderr
        assert "running pid=" in status.stdout

        stopped = subprocess.run(
            [
                str(bridge),
                "--stop-daemon",
                "--config",
                str(config),
                "--runtime-dir",
                str(runtime_dir),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
        assert stopped.returncode == 0, stopped.stderr
        assert stopped.stdout.strip() == "stopped"
        assert daemon.wait(timeout=20) == 0
        assert not endpoint.exists()
    finally:
        if daemon.poll() is None:
            daemon.terminate()
            try:
                daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=10)


def test_native_bridge_auto_starts_and_stops_daemon(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    bridge = _bridge_binary(project_root)
    if not bridge.is_file():
        pytest.skip("Native bridge release binary is required")

    config = tmp_path / "config.yaml"
    config.write_text(
        "api:\n  data_dir: ./data\nlocal_acp:\n  session_store_path: ./data/sessions.db\n",
        encoding="utf-8",
    )
    runtime_dir = tmp_path / "runtime"
    environment = os.environ.copy()
    environment["DEER_FLOW_ACP_DAEMON_WARMUP"] = "0"
    environment["DEER_FLOW_ACP_DAEMON_SANDBOX_WARMUP"] = "0"
    environment["DEER_FLOW_ACP_DAEMON_START_TIMEOUT_MS"] = "20000"

    started = subprocess.run(
        [
            str(bridge),
            "--start-daemon",
            "--python",
            sys.executable,
            "--config",
            str(config),
            "--runtime-dir",
            str(runtime_dir),
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    try:
        assert started.returncode == 0, started.stderr
        assert "running pid=" in started.stdout
    finally:
        stopped = subprocess.run(
            [
                str(bridge),
                "--stop-daemon",
                "--config",
                str(config),
                "--runtime-dir",
                str(runtime_dir),
            ],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        assert stopped.returncode == 0, stopped.stderr


def test_native_bridge_does_not_delete_temporarily_unresponsive_endpoint(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    bridge = _bridge_binary(project_root)
    if not bridge.is_file():
        pytest.skip("Native bridge release binary is required")

    config = tmp_path / "config.yaml"
    config.write_text("api:\n  data_dir: ./data\n", encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        unavailable_port = probe.getsockname()[1]
    endpoint = runtime_dir / "endpoint.json"
    payload = {
        "host": "127.0.0.1",
        "port": unavailable_port,
        "token": "temporarily-unresponsive",
        "pid": os.getpid(),
        "build_id": "test",
        "config_path": str(config.resolve()),
    }
    endpoint.write_text(json.dumps(payload), encoding="utf-8")

    result = subprocess.run(
        [
            str(bridge),
            "--config",
            str(config),
            "--runtime-dir",
            str(runtime_dir),
            "--no-auto-start",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert endpoint.exists()
    assert json.loads(endpoint.read_text(encoding="utf-8")) == payload
