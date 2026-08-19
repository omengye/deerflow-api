from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_real_stdio_initialize_new_and_list(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_bytes(
        (project_root / "resources" / "default-config.yaml").read_bytes()
    )
    (config_dir / "extensions_config.json").write_text(
        json.dumps({"mcpServers": {}, "skills": {}}), encoding="utf-8"
    )
    environment = os.environ.copy()
    environment["DEER_FLOW_ACP_CHECKPOINTER_PATH"] = str(tmp_path / "checkpoints.db")
    environment["DEER_FLOW_ACP_SESSION_STORE_PATH"] = str(tmp_path / "sessions.db")
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(project_root), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    process = subprocess.Popen(
        [sys.executable, "-m", "deerflow.acp", "--config", str(config_path)],
        # Simulate Zed launching the agent from an unrelated user workspace.
        cwd=tmp_path,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert process.stdin is not None
    assert process.stdout is not None

    def request(request_id: int, method: str, params: dict[str, object]) -> dict[str, object]:
        process.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            + "\n"
        )
        process.stdin.flush()
        response = process.stdout.readline()
        if not response:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise AssertionError(f"ACP stdio process exited before replying: {stderr}")
        return json.loads(response)

    initialized = request(
        1,
        "initialize",
        {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    )
    assert initialized["result"]["protocolVersion"] == 1  # type: ignore[index]
    assert (
        initialized["result"]["agentCapabilities"]["promptCapabilities"]["image"]  # type: ignore[index]
        is True
    )

    created = request(
        2,
        "session/new",
        {"cwd": str(tmp_path), "mcpServers": []},
    )
    session_id = created["result"]["sessionId"]  # type: ignore[index]
    assert session_id

    listed = request(3, "session/list", {"cwd": str(tmp_path)})
    sessions = listed["result"]["sessions"]  # type: ignore[index]
    assert [session["sessionId"] for session in sessions] == [session_id]

    process.stdin.close()
    return_code = process.wait(timeout=20)
    stderr = process.stderr.read() if process.stderr is not None else ""
    assert return_code == 0, stderr
