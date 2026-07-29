from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from app.config import ThreadCleanupSettings
from app.thread_cleanup import (
    ThreadCleanupInProgressError,
    ThreadCleanupService,
    _parse_iso,
)


class FakeScheduleStore:
    def __init__(self) -> None:
        self.scheduled_threads: set[str] = set()

    async def list_tasks(
        self,
        *,
        thread_id: str,
        include_disabled: bool,
        limit: int,
    ) -> list[object]:
        del include_disabled, limit
        return [object()] if thread_id in self.scheduled_threads else []


class FakeManager:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.running_threads: set[str] = set()
        self.deleted_threads: list[str] = []
        self.schedule_store = FakeScheduleStore()
        self.scheduler_service = SimpleNamespace(store=self.schedule_store)

    def is_thread_running(self, thread_id: str) -> bool:
        return thread_id in self.running_threads

    def delete_thread_completely(self, thread_id: str) -> dict[str, object]:
        self.deleted_threads.append(thread_id)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
            conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
        return {"success": True}


def _create_checkpoint_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        SqliteSaver(conn).setup()
    finally:
        conn.close()


def _insert_checkpoint(
    path: Path,
    thread_id: str,
    *,
    timestamp: datetime | None = None,
    valid: bool = True,
    with_write: bool = False,
) -> None:
    if valid:
        type_, checkpoint = JsonPlusSerializer().dumps_typed(
            {"ts": (timestamp or datetime.now(UTC)).isoformat(), "payload": "x" * 128}
        )
    else:
        type_, checkpoint = "invalid", b"not-a-checkpoint"
    checkpoint_id = f"cp-{thread_id}"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO checkpoints (
                thread_id, checkpoint_ns, checkpoint_id,
                parent_checkpoint_id, type, checkpoint, metadata
            ) VALUES (?, '', ?, NULL, ?, ?, ?)
            """,
            (thread_id, checkpoint_id, type_, checkpoint, b"{}"),
        )
        if with_write:
            conn.execute(
                """
                INSERT INTO writes (
                    thread_id, checkpoint_ns, checkpoint_id,
                    task_id, idx, channel, type, value
                ) VALUES (?, '', ?, 'task', 0, 'messages', 'bytes', ?)
                """,
                (thread_id, checkpoint_id, b"y" * 256),
            )


def _service(path: Path, **overrides: object) -> tuple[ThreadCleanupService, FakeManager]:
    _create_checkpoint_database(path)
    manager = FakeManager(path)
    config = ThreadCleanupSettings(**overrides)
    service = ThreadCleanupService(db_path=path, manager=manager, config=config)
    service.setup()
    return service, manager


def _activity(path: Path, thread_id: str) -> sqlite3.Row | None:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM thread_activity WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()


def test_setup_touch_and_legacy_backfill(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.db"
    service, _ = _service(db_path)
    old = datetime.now(UTC) - timedelta(days=90)
    recent = datetime.now(UTC) - timedelta(hours=1)
    _insert_checkpoint(db_path, "legacy-valid", timestamp=old)
    _insert_checkpoint(db_path, "legacy-corrupt", valid=False)

    service.touch_thread_sync("runtime-thread", at=recent, source="create")
    service.touch_thread_sync("runtime-thread", at=old, source="late_event")
    assert service.backfill_activity_sync() == 2

    runtime = _activity(db_path, "runtime-thread")
    valid = _activity(db_path, "legacy-valid")
    corrupt = _activity(db_path, "legacy-corrupt")
    assert runtime is not None
    assert _parse_iso(runtime["last_activity_at"]) == recent
    assert valid is not None
    assert valid["source"] == "checkpoint_backfill"
    assert valid["protected"] == 0
    assert _parse_iso(valid["last_activity_at"]) == old
    assert corrupt is not None
    assert corrupt["source"] == "backfill_failed"
    assert corrupt["protected"] == 1


@pytest.mark.asyncio
async def test_preview_uses_activity_cutoff_and_protects_active_threads(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.db"
    service, manager = _service(db_path, inactive_days=30)
    old = datetime.now(UTC) - timedelta(days=60)
    recent = datetime.now(UTC) - timedelta(days=2)

    for thread_id in ("eligible", "running", "scheduled"):
        service.touch_thread_sync(thread_id, at=old)
        _insert_checkpoint(db_path, thread_id, timestamp=old, with_write=True)
    service.touch_thread_sync("recent", at=recent)
    _insert_checkpoint(db_path, "recent", timestamp=recent)
    manager.running_threads.add("running")
    manager.schedule_store.scheduled_threads.add("scheduled")

    preview = await service.preview(limit=20)

    candidates = {item["thread_id"]: item for item in preview["candidates"]}
    assert set(candidates) == {"eligible", "running", "scheduled"}
    assert candidates["running"]["running"] is True
    assert candidates["scheduled"]["scheduled"] is True
    assert preview["eligible_count"] == 1
    assert preview["estimated_reclaimable_bytes"] == candidates["eligible"]["estimated_bytes"]


@pytest.mark.asyncio
async def test_background_cleanup_deletes_only_eligible_threads(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.db"
    service, manager = _service(db_path, inactive_days=30, batch_size=1)
    old = datetime.now(UTC) - timedelta(days=60)

    for thread_id in ("eligible", "running", "scheduled"):
        service.touch_thread_sync(thread_id, at=old)
        _insert_checkpoint(db_path, thread_id, timestamp=old, with_write=True)
    manager.running_threads.add("running")
    manager.schedule_store.scheduled_threads.add("scheduled")

    started = await service.start_run()
    assert started["status"] == "pending"
    assert service._job_task is not None
    await service._job_task

    status = await service.status()
    assert manager.deleted_threads == ["eligible"]
    assert _activity(db_path, "eligible") is None
    assert _activity(db_path, "running") is not None
    assert _activity(db_path, "scheduled") is not None
    assert status["last_run"]["status"] == "completed"
    assert status["last_run"]["deleted"] == 1
    assert status["last_run"]["skipped"] == 2
    assert status["database"]["checkpoint_rows"] == 2
    assert status["database"]["write_rows"] == 2
    assert status["database"]["database_bytes"] > 0
    assert status["database"]["reusable_bytes"] >= 0


@pytest.mark.asyncio
async def test_dry_run_persists_result_without_deleting(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.db"
    service, manager = _service(db_path, inactive_days=30)
    old = datetime.now(UTC) - timedelta(days=60)
    service.touch_thread_sync("dry-run", at=old)
    _insert_checkpoint(db_path, "dry-run", timestamp=old)

    await service.start_run(dry_run=True)
    assert service._job_task is not None
    await service._job_task

    status = await service.status()
    assert manager.deleted_threads == []
    assert _activity(db_path, "dry-run") is not None
    assert status["last_run"]["dry_run"] == 1
    assert status["last_run"]["scanned"] == 1
    assert status["last_run"]["skipped"] == 1


@pytest.mark.asyncio
async def test_concurrent_starts_share_one_job(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.db"
    service, _ = _service(db_path)
    release = asyncio.Event()

    async def held_run(*, cutoff: datetime, limit: int) -> None:
        del cutoff, limit
        await release.wait()

    service._execute_run = held_run  # type: ignore[method-assign]
    first, second = await asyncio.gather(service.start_run(), service.start_run())

    assert first["job_id"] == second["job_id"]
    assert second["already_running"] is True
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM thread_cleanup_runs").fetchone()[0] == 1

    release.set()
    assert service._job_task is not None
    await service._job_task


def test_next_run_respects_configured_timezone(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.db"
    service, _ = _service(db_path, run_daily_at="03:30", timezone="Asia/Shanghai")

    before_target = datetime(2026, 1, 1, 18, 0, tzinfo=UTC)  # 02:00 next day in Shanghai
    after_target = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)  # 04:00 next day in Shanghai

    assert service._next_run_at(before_target) == datetime(2026, 1, 1, 19, 30, tzinfo=UTC)
    assert service._next_run_at(after_target) == datetime(2026, 1, 2, 19, 30, tzinfo=UTC)


def test_thread_claim_and_foreground_touch_are_mutually_exclusive(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.db"
    service, _ = _service(db_path)
    old = datetime.now(UTC) - timedelta(days=60)
    cutoff = datetime.now(UTC) - timedelta(days=30)
    service.touch_thread_sync("claimed", at=old)

    assert service._claim_thread_sync("claimed", cutoff=cutoff, job_id="job-1") is True
    with pytest.raises(ThreadCleanupInProgressError):
        service.touch_thread_sync("claimed", source="chat")

    service._release_thread_claim_sync("claimed", "job-1")
    service.touch_thread_sync("claimed", source="chat")
    assert service._claim_thread_sync("claimed", cutoff=cutoff, job_id="job-2") is False


def test_database_lease_allows_only_one_service_instance(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.db"
    first, _ = _service(db_path)
    second_manager = FakeManager(db_path)
    second = ThreadCleanupService(
        db_path=db_path,
        manager=second_manager,
        config=ThreadCleanupSettings(),
    )
    second.setup()

    assert first._acquire_job_lease_sync("job-1") == (True, None)
    assert second._acquire_job_lease_sync("job-2") == (False, "job-1")
    first._release_job_lease_sync("job-1")
    assert second._acquire_job_lease_sync("job-2")[0] is True


@pytest.mark.asyncio
async def test_scheduled_run_is_deferred_during_quiet_period(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.db"
    service, _ = _service(
        db_path,
        quiet_period_minutes=15,
        postpone_minutes=7,
    )
    service.touch_thread_sync("recent", source="chat")

    result = await service.start_run(trigger="scheduled")

    assert result["status"] == "deferred"
    assert result["reason"] == "recent_activity"
    assert _parse_iso(result["retry_at"]) > datetime.now(UTC) + timedelta(minutes=6)
    assert service._active_run_sync() is None
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM thread_cleanup_runs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_cleanup_stops_when_new_foreground_activity_appears(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.db"
    service, manager = _service(
        db_path,
        batch_size=20,
        batch_interval_seconds=0,
        stop_on_new_activity=True,
    )
    old = datetime.now(UTC) - timedelta(days=60)
    for thread_id in ("a-old", "b-old"):
        service.touch_thread_sync(thread_id, at=old)
        _insert_checkpoint(db_path, thread_id, timestamp=old)

    original_delete = manager.delete_thread_completely

    def delete_and_create_activity(thread_id: str) -> dict[str, object]:
        result = original_delete(thread_id)
        if len(manager.deleted_threads) == 1:
            service.touch_thread_sync("new-activity", source="chat")
        return result

    manager.delete_thread_completely = delete_and_create_activity  # type: ignore[method-assign]
    await service.start_run(limit=2)
    assert service._job_task is not None
    await service._job_task

    assert manager.deleted_threads == ["a-old"]
    assert service._last_run_sync()["status"] == "stopped_on_activity"


@pytest.mark.asyncio
async def test_protected_candidates_do_not_starve_later_eligible_thread(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.db"
    service, manager = _service(
        db_path,
        batch_size=20,
        batch_interval_seconds=0,
        stop_on_new_activity=False,
    )
    old = datetime.now(UTC) - timedelta(days=60)
    for index in range(205):
        thread_id = f"a-scheduled-{index:03d}"
        service.touch_thread_sync(thread_id, at=old)
        manager.schedule_store.scheduled_threads.add(thread_id)
    service.touch_thread_sync("z-eligible", at=old)

    await service.start_run(limit=1)
    assert service._job_task is not None
    await service._job_task

    run = service._last_run_sync()
    assert manager.deleted_threads == ["z-eligible"]
    assert run["deleted"] == 1
    assert run["skipped"] == 205
    assert run["scanned"] == 206


@pytest.mark.asyncio
async def test_deletion_limit_counts_successes_not_failed_attempts(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.db"
    service, manager = _service(
        db_path,
        batch_size=10,
        batch_interval_seconds=0,
        stop_on_new_activity=False,
    )
    old = datetime.now(UTC) - timedelta(days=60)
    for thread_id in ("a-fails", "b-fails", "c-succeeds"):
        service.touch_thread_sync(thread_id, at=old)

    original_delete = manager.delete_thread_completely

    def fail_first_two(thread_id: str) -> dict[str, object]:
        if thread_id.endswith("fails"):
            return {"success": False, "detail": "injected failure"}
        return original_delete(thread_id)

    manager.delete_thread_completely = fail_first_two  # type: ignore[method-assign]
    await service.start_run(limit=1)
    assert service._job_task is not None
    await service._job_task

    run = service._last_run_sync()
    assert manager.deleted_threads == ["c-succeeds"]
    assert run["deleted"] == 1
    assert run["failed"] == 2
    assert run["status"] == "completed_with_errors"


@pytest.mark.asyncio
async def test_batch_interval_is_applied_between_real_delete_batches(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.db"
    service, manager = _service(
        db_path,
        batch_size=1,
        batch_interval_seconds=0.05,
        stop_on_new_activity=False,
    )
    old = datetime.now(UTC) - timedelta(days=60)
    for thread_id in ("batch-a", "batch-b"):
        service.touch_thread_sync(thread_id, at=old)

    started = time.perf_counter()
    await service.start_run(limit=2)
    assert service._job_task is not None
    await service._job_task
    elapsed = time.perf_counter() - started

    assert manager.deleted_threads == ["batch-a", "batch-b"]
    assert elapsed >= 0.04


@pytest.mark.asyncio
async def test_disabled_service_does_not_backfill_in_maintenance_loop(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.db"
    service, _ = _service(db_path, enabled=False)
    calls = 0

    def counted_backfill(*, limit: int = 500) -> int:
        nonlocal calls
        del limit
        calls += 1
        return 0

    service.backfill_activity_sync = counted_backfill  # type: ignore[method-assign]
    task = asyncio.create_task(service._run_loop())
    await asyncio.sleep(0)
    service._stopping.set()
    service._wake.set()
    await task

    assert calls == 0


@pytest.mark.asyncio
async def test_persisted_schedule_is_protected_when_scheduler_worker_is_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.config import settings

    db_path = tmp_path / "checkpoints.db"
    service, manager = _service(db_path)
    manager.scheduler_service = None
    scheduler_path = tmp_path / "scheduled_tasks.db"
    with sqlite3.connect(scheduler_path) as conn:
        conn.execute(
            "CREATE TABLE scheduled_tasks (thread_id TEXT NOT NULL, enabled INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO scheduled_tasks(thread_id, enabled) VALUES ('scheduled', 1)"
        )
    monkeypatch.setattr(settings, "scheduler_db_path", str(scheduler_path))
    monkeypatch.setattr(settings, "config_path", str(tmp_path / "config.yaml"))

    assert await service._has_enabled_schedule("scheduled") is True
    assert await service._has_enabled_schedule("not-scheduled") is False


def test_large_database_metrics_skip_expensive_global_row_counts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "checkpoints.db"
    service, _ = _service(db_path)
    monkeypatch.setattr("app.thread_cleanup._EXACT_ROW_COUNT_MAX_BYTES", 0)

    metrics = service.database_metrics_sync()

    assert metrics["row_counts_exact"] is False
    assert metrics["checkpoint_rows"] is None
    assert metrics["write_rows"] is None


def test_setup_migrates_cleanup_tables_created_by_older_release(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.db"
    _create_checkpoint_database(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE thread_activity (
                thread_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                last_activity_at TEXT NOT NULL,
                protected INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'runtime',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE thread_cleanup_runs (
                job_id TEXT PRIMARY KEY,
                trigger TEXT NOT NULL,
                dry_run INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                cutoff_at TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                scanned INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                estimated_reclaimed_bytes INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            """
        )
    service = ThreadCleanupService(
        db_path=db_path,
        manager=FakeManager(db_path),
        config=ThreadCleanupSettings(),
    )

    service.setup()

    with sqlite3.connect(db_path) as conn:
        activity_columns = {row[1] for row in conn.execute("PRAGMA table_info(thread_activity)")}
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(thread_cleanup_runs)")}
        assert {"cleanup_claimed_by", "cleanup_claim_expires_at"} <= activity_columns
        assert {"current_thread_id", "deletion_limit"} <= run_columns
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'thread_cleanup_lease'"
        ).fetchone()
