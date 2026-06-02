"""Tests for ClientManager.delete_thread_completely filesystem cleanup.

The AIO sandbox runs its container as root, leaving root-owned files in the
bind-mounted thread directory that the backend (a non-root process) cannot
delete. delete_thread_completely must fall back to a root container to remove
them instead of silently failing.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from app.dependencies import ClientManager


class _FakePaths:
    """Stand-in for deerflow.config.paths.Paths used by the cleanup code."""

    def __init__(self, thread_dir: Path, host_thread_dir: str) -> None:
        self._thread_dir = thread_dir
        self._host_thread_dir = host_thread_dir

    def thread_dir(self, thread_id: str) -> Path:
        return self._thread_dir

    def host_thread_dir(self, thread_id: str) -> str:
        return self._host_thread_dir


def _make_manager() -> ClientManager:
    cm = ClientManager()
    # Avoid touching a real checkpointer.
    cm.get_checkpointer = lambda: None  # type: ignore[method-assign]
    return cm


def test_cleanup_falls_back_to_root_container_on_permission_error(tmp_path, monkeypatch):
    thread_id = "feishu_oc_abc123"
    thread_dir = tmp_path / "threads" / thread_id
    thread_dir.mkdir(parents=True)
    host_thread_dir = f"/host/data/threads/{thread_id}"

    fake_paths = _FakePaths(thread_dir, host_thread_dir)
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: fake_paths)

    # Simulate the host process being unable to delete root-owned files.
    def _raise_permission(_path):
        raise PermissionError(13, "Permission denied", str(thread_dir / "user-data" / "workspace"))

    monkeypatch.setattr("shutil.rmtree", _raise_permission)

    calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        calls.append(args)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)

    result = _make_manager().delete_thread_completely(thread_id)

    assert result["success"] is True
    assert len(calls) == 1
    args = calls[0]
    # A throwaway root container mounting the PARENT and removing only the target.
    assert args[:3] == ["docker", "run", "--rm"]
    assert "-v" in args
    assert f"/host/data/threads:/target" in args
    assert args[-3:] == ["rm", "-rf", f"/target/{thread_id}"]


def test_cleanup_uses_plain_rmtree_when_permitted(tmp_path, monkeypatch):
    thread_id = "feishu_oc_ok"
    thread_dir = tmp_path / "threads" / thread_id
    thread_dir.mkdir(parents=True)

    fake_paths = _FakePaths(thread_dir, f"/host/data/threads/{thread_id}")
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: fake_paths)

    removed: list[Path] = []
    monkeypatch.setattr("shutil.rmtree", lambda p: removed.append(p))

    def _fail_run(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("root container fallback should not be invoked")

    monkeypatch.setattr("subprocess.run", _fail_run)

    result = _make_manager().delete_thread_completely(thread_id)

    assert result["success"] is True
    assert removed == [thread_dir]
