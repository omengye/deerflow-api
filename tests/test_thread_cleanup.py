"""Tests for ClientManager.delete_thread_completely filesystem cleanup.

The AIO sandbox runs its container as root, leaving root-owned files in the
bind-mounted thread directory that the backend (a non-root process) cannot
delete. delete_thread_completely must fall back to a root container to remove
them instead of silently failing.
"""

from __future__ import annotations

import asyncio
import io
import types
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

from app.dependencies import ClientManager
from app.thread_cleanup import ThreadCleanupInProgressError
from deerflow.runtime import ConflictError


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


def test_cleanup_retires_mcp_sandbox_and_scheduler_resources(tmp_path, monkeypatch):
    manager = _make_manager()
    thread_id = "retire-all"
    thread_dir = tmp_path / "threads" / thread_id
    thread_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "deerflow.config.paths.get_paths",
        lambda: _FakePaths(thread_dir, f"/host/data/threads/{thread_id}"),
    )

    closed_scopes: list[str] = []
    released_threads: list[str] = []
    deleted_schedule_threads: list[str] = []

    class Pool:
        async def close_scope(self, scope: str) -> None:
            closed_scopes.append(scope)

    class SandboxProvider:
        def release_thread(self, scope: str) -> None:
            released_threads.append(scope)

    class Scheduler:
        async def delete_tasks_for_thread(self, scope: str) -> int:
            deleted_schedule_threads.append(scope)
            return 2

    manager.scheduler_service = Scheduler()
    monkeypatch.setattr("deerflow.mcp.session_pool.get_session_pool", lambda: Pool())
    monkeypatch.setattr(
        "deerflow.sandbox.sandbox_provider.get_existing_sandbox_provider",
        lambda: SandboxProvider(),
    )

    result = manager.delete_thread_completely(thread_id)

    assert result["success"] is True
    assert closed_scopes == [thread_id]
    assert released_threads == [thread_id]
    assert deleted_schedule_threads == [thread_id]


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
    assert "/host/data/threads:/target" in args
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


@pytest.mark.asyncio
async def test_touch_failure_does_not_leave_pending_run_record() -> None:
    manager = ClientManager()

    class RejectingCleanupService:
        async def touch_thread(self, thread_id: str, *, source: str) -> None:
            del source
            raise ThreadCleanupInProgressError(f"Thread {thread_id} is being deleted")

    manager.thread_cleanup_service = RejectingCleanupService()

    with pytest.raises(ConflictError):
        await manager.start_client_stream_run(
            thread_id="claimed-thread",
            run_id="run-touch-failed",
            message="hello",
            kwargs={},
        )

    assert manager.run_manager.get("run-touch-failed") is None
    assert await manager.run_manager.has_inflight("claimed-thread") is False
    assert manager.is_thread_running("claimed-thread") is False


@pytest.mark.asyncio
async def test_run_record_is_rolled_back_if_delete_starts_after_touch() -> None:
    manager = ClientManager()
    with manager._thread_lock:
        manager._deleting_threads.add("deleting-thread")

    with pytest.raises(ConflictError):
        await manager.start_client_stream_run(
            thread_id="deleting-thread",
            run_id="run-delete-race",
            message="hello",
            kwargs={},
        )

    assert manager.run_manager.get("run-delete-race") is None
    assert await manager.run_manager.has_inflight("deleting-thread") is False


@pytest.mark.asyncio
async def test_run_record_and_running_marker_roll_back_if_task_creation_fails(
    monkeypatch,
) -> None:
    manager = ClientManager()

    def fail_create_task(_coroutine):
        raise RuntimeError("injected task creation failure")

    monkeypatch.setattr("app.dependencies.asyncio.create_task", fail_create_task)
    with pytest.raises(RuntimeError, match="injected task creation failure"):
        await manager.start_client_stream_run(
            thread_id="task-failure-thread",
            run_id="run-task-failed",
            message="hello",
            kwargs={},
        )

    assert manager.run_manager.get("run-task-failed") is None
    assert manager.is_thread_running("task-failure-thread") is False


@pytest.mark.asyncio
async def test_slow_delete_does_not_hold_run_state_lock(tmp_path, monkeypatch) -> None:
    manager = _make_manager()
    thread_id = "slow-delete"
    thread_dir = tmp_path / "threads" / thread_id
    thread_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "deerflow.config.paths.get_paths",
        lambda: _FakePaths(thread_dir, f"/host/data/threads/{thread_id}"),
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_rmtree(_path) -> None:
        loop.call_soon_threadsafe(entered.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=2)

    monkeypatch.setattr("shutil.rmtree", slow_rmtree)
    delete_task = asyncio.create_task(
        asyncio.to_thread(manager.delete_thread_completely, thread_id)
    )
    await asyncio.wait_for(entered.wait(), timeout=1)

    # This call takes the same lock used by mark_thread_running/delete state.
    # It must remain responsive while filesystem deletion is blocked.
    assert await asyncio.wait_for(
        asyncio.to_thread(manager.is_thread_running, "unrelated-thread"),
        timeout=0.2,
    ) is False
    release.set()
    result = await delete_task
    assert result["success"] is True


@pytest.mark.asyncio
async def test_upload_touch_failure_happens_before_persistent_upload(monkeypatch) -> None:
    upload_calls: list[str] = []

    class FakeClient:
        def upload_files(self, thread_id: str, _paths) -> dict[str, object]:
            upload_calls.append(thread_id)
            return {"success": True}

    class FakeManager:
        def get_client(self) -> FakeClient:
            return FakeClient()

        async def touch_thread_activity(self, thread_id: str, *, source: str) -> None:
            del source
            raise ThreadCleanupInProgressError(f"Thread {thread_id} is being deleted")

    monkeypatch.setattr("app.routers.uploads.get_client_manager", lambda: FakeManager())
    from app.routers.uploads import upload_files

    incoming = UploadFile(filename="note.txt", file=io.BytesIO(b"hello"))
    with pytest.raises(HTTPException) as exc_info:
        await upload_files("claimed-thread", [incoming])

    assert exc_info.value.status_code == 409
    assert upload_calls == []
