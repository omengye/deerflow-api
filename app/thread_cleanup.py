"""Indexed, non-blocking cleanup for inactive persisted conversations."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import ThreadCleanupSettings

logger = logging.getLogger(__name__)

_BACKFILL_BATCH_SIZE = 500
_JOB_LEASE_SECONDS = 10 * 60
_THREAD_CLAIM_SECONDS = 10 * 60
_FOREGROUND_BUSY_TIMEOUT_MS = 5_000
_LEASE_NAME = "inactive-thread-cleanup"
_EXACT_ROW_COUNT_MAX_BYTES = 512 * 1024 * 1024


class ThreadCleanupInProgressError(RuntimeError):
    """Raised when foreground activity targets a thread being deleted."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(UTC).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name in {"Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin"}:
            return ZoneInfo("Asia/Shanghai")
        raise ValueError(f"Unknown timezone: {name}") from None


class ThreadCleanupService:
    """Maintain last-activity metadata and delete inactive threads in batches."""

    def __init__(self, *, db_path: Path, manager: Any, config: ThreadCleanupSettings) -> None:
        self.db_path = db_path.resolve()
        self.manager = manager
        self.config = config
        self._loop_task: asyncio.Task | None = None
        self._job_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._activity_event = asyncio.Event()
        self._job_status: dict[str, Any] | None = None
        self._next_scheduled_at: datetime | None = None

    def _connect(self, *, busy_timeout_ms: int = 30_000) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        return conn

    def setup(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS thread_activity (
                    thread_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    last_activity_at TEXT NOT NULL,
                    protected INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'runtime',
                    updated_at TEXT NOT NULL,
                    cleanup_claimed_by TEXT,
                    cleanup_claim_expires_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_thread_activity_last_activity
                ON thread_activity(last_activity_at);

                CREATE TABLE IF NOT EXISTS thread_cleanup_runs (
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
                    current_thread_id TEXT,
                    deletion_limit INTEGER,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_thread_cleanup_runs_started
                ON thread_cleanup_runs(started_at DESC);

                CREATE TABLE IF NOT EXISTS thread_cleanup_lease (
                    name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS thread_cleanup_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            activity_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(thread_activity)")
            }
            if "cleanup_claimed_by" not in activity_columns:
                conn.execute("ALTER TABLE thread_activity ADD COLUMN cleanup_claimed_by TEXT")
            if "cleanup_claim_expires_at" not in activity_columns:
                conn.execute(
                    "ALTER TABLE thread_activity ADD COLUMN cleanup_claim_expires_at TEXT"
                )
            run_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(thread_cleanup_runs)")
            }
            if "current_thread_id" not in run_columns:
                conn.execute(
                    "ALTER TABLE thread_cleanup_runs ADD COLUMN current_thread_id TEXT"
                )
            if "deletion_limit" not in run_columns:
                conn.execute(
                    "ALTER TABLE thread_cleanup_runs ADD COLUMN deletion_limit INTEGER"
                )

    async def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        await asyncio.to_thread(self.setup)
        self._stopping.clear()
        self._loop_task = asyncio.create_task(self._run_loop(), name="deerflow-thread-cleanup")
        logger.info(
            "Thread cleanup service started (enabled=%s inactive_days=%d daily_at=%s %s)",
            self.config.enabled,
            self.config.inactive_days,
            self.config.run_daily_at,
            self.config.timezone,
        )

    async def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        if self._job_task is not None and not self._job_task.done():
            self._job_task.cancel()
            await asyncio.gather(self._job_task, return_exceptions=True)
        self._job_task = None
        logger.info("Thread cleanup service stopped")

    async def reconfigure(self, config: ThreadCleanupSettings) -> None:
        _timezone(config.timezone)
        self.config = config
        self._next_scheduled_at = None
        self._wake.set()
        logger.info("Thread cleanup configuration updated: %s", config.model_dump())

    def touch_thread_sync(self, thread_id: str, *, at: datetime | None = None, source: str = "runtime") -> None:
        if not thread_id:
            return
        stamp = _iso(at)
        with self._connect(busy_timeout_ms=_FOREGROUND_BUSY_TIMEOUT_MS) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT cleanup_claimed_by, cleanup_claim_expires_at
                FROM thread_activity WHERE thread_id = ?
                """,
                (thread_id,),
            ).fetchone()
            if existing and existing["cleanup_claimed_by"]:
                claim_expires = _parse_iso(existing["cleanup_claim_expires_at"])
                if claim_expires is None or claim_expires > _utc_now():
                    raise ThreadCleanupInProgressError(
                        f"Thread {thread_id} is currently being cleaned up"
                    )
            conn.execute(
                """
                INSERT INTO thread_activity (
                    thread_id, created_at, last_activity_at, protected, source,
                    updated_at, cleanup_claimed_by, cleanup_claim_expires_at
                ) VALUES (?, ?, ?, 0, ?, ?, NULL, NULL)
                ON CONFLICT(thread_id) DO UPDATE SET
                    last_activity_at = CASE
                        WHEN excluded.last_activity_at > thread_activity.last_activity_at
                        THEN excluded.last_activity_at
                        ELSE thread_activity.last_activity_at
                    END,
                    source = excluded.source,
                    updated_at = excluded.updated_at,
                    cleanup_claimed_by = NULL,
                    cleanup_claim_expires_at = NULL
                """,
                (thread_id, stamp, stamp, source, stamp),
            )

    async def touch_thread(self, thread_id: str, *, source: str = "runtime") -> None:
        await asyncio.to_thread(self.touch_thread_sync, thread_id, source=source)
        self._activity_event.set()

    def forget_thread_sync(self, thread_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM thread_activity WHERE thread_id = ?", (thread_id,))

    def _deserialize_checkpoint_timestamp(self, type_: str | None, blob: bytes | None) -> datetime | None:
        if not type_ or blob is None:
            return None
        try:
            from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

            checkpoint = JsonPlusSerializer(pickle_fallback=True).loads_typed((type_, blob))
            if isinstance(checkpoint, dict):
                return _parse_iso(str(checkpoint.get("ts") or ""))
        except Exception:
            logger.debug("Could not deserialize checkpoint timestamp during activity backfill", exc_info=True)
        return None

    def backfill_activity_sync(self, *, limit: int = _BACKFILL_BATCH_SIZE) -> int:
        """Index legacy threads without deserializing their full history."""
        now = _iso()
        with self._connect() as conn:
            complete = conn.execute(
                "SELECT value FROM thread_cleanup_meta WHERE key = 'backfill_complete'"
            ).fetchone()
            if complete and complete["value"] == "1":
                return 0
            cursor_row = conn.execute(
                "SELECT value FROM thread_cleanup_meta WHERE key = 'backfill_cursor'"
            ).fetchone()
            cursor = cursor_row["value"] if cursor_row else ""
            rows = conn.execute(
                """
                SELECT c.thread_id, c.checkpoint_id, c.type, c.checkpoint
                FROM checkpoints AS c
                JOIN (
                    SELECT cp.thread_id, MAX(cp.checkpoint_id) AS checkpoint_id
                    FROM checkpoints AS cp
                    LEFT JOIN thread_activity AS a ON a.thread_id = cp.thread_id
                    WHERE a.thread_id IS NULL AND cp.thread_id > ?
                    GROUP BY cp.thread_id
                    ORDER BY cp.thread_id
                    LIMIT ?
                ) AS latest
                  ON latest.thread_id = c.thread_id
                 AND latest.checkpoint_id = c.checkpoint_id
                GROUP BY c.thread_id
                ORDER BY c.thread_id
                """,
                (cursor, max(1, limit)),
            ).fetchall()
            if not rows:
                conn.execute(
                    """
                    INSERT INTO thread_cleanup_meta(key, value)
                    VALUES ('backfill_complete', '1')
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """
                )
                conn.execute(
                    "DELETE FROM thread_cleanup_meta WHERE key = 'backfill_cursor'"
                )
                return 0

            inserts: list[tuple[Any, ...]] = []
            for row in rows:
                timestamp = self._deserialize_checkpoint_timestamp(row["type"], row["checkpoint"])
                if timestamp is None:
                    # Unknown legacy records are protected instead of being
                    # assigned an invented old date and deleted automatically.
                    inserts.append((row["thread_id"], now, now, 1, "backfill_failed", now))
                    continue
                stamp = _iso(timestamp)
                inserts.append((row["thread_id"], stamp, stamp, 0, "checkpoint_backfill", now))

            conn.executemany(
                """
                INSERT OR IGNORE INTO thread_activity (
                    thread_id, created_at, last_activity_at, protected, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                inserts,
            )
            conn.execute(
                """
                INSERT INTO thread_cleanup_meta(key, value)
                VALUES ('backfill_cursor', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (rows[-1]["thread_id"],),
            )
            return len(inserts)

    def _candidate_rows_sync(
        self,
        *,
        cutoff: datetime,
        limit: int,
        after: tuple[str, str] | None = None,
        include_storage_stats: bool = True,
    ) -> list[dict[str, Any]]:
        cutoff_text = _iso(cutoff)
        now_text = _iso()
        with self._connect() as conn:
            cursor_sql = ""
            params: list[Any] = [cutoff_text, now_text]
            if after is not None:
                cursor_sql = (
                    "AND (last_activity_at > ? OR "
                    "(last_activity_at = ? AND thread_id > ?))"
                )
                params.extend((after[0], after[0], after[1]))
            params.append(max(1, limit))
            activities = conn.execute(
                f"""
                SELECT thread_id, last_activity_at, protected, source
                FROM thread_activity
                WHERE protected = 0 AND last_activity_at < ?
                  AND (
                    cleanup_claimed_by IS NULL
                    OR cleanup_claim_expires_at IS NULL
                    OR cleanup_claim_expires_at <= ?
                  )
                  {cursor_sql}
                ORDER BY last_activity_at ASC, thread_id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
            candidates: list[dict[str, Any]] = []
            for activity in activities:
                thread_id = activity["thread_id"]
                checkpoint_rows = write_rows = estimated_bytes = 0
                if include_storage_stats:
                    checkpoint = conn.execute(
                        """
                        SELECT COUNT(*) AS rows,
                               COALESCE(SUM(LENGTH(checkpoint) + LENGTH(metadata)), 0) AS bytes
                        FROM checkpoints WHERE thread_id = ?
                        """,
                        (thread_id,),
                    ).fetchone()
                    writes = conn.execute(
                        """
                        SELECT COUNT(*) AS rows, COALESCE(SUM(LENGTH(value)), 0) AS bytes
                        FROM writes WHERE thread_id = ?
                        """,
                        (thread_id,),
                    ).fetchone()
                    checkpoint_rows = int(checkpoint["rows"] or 0)
                    write_rows = int(writes["rows"] or 0)
                    estimated_bytes = int(checkpoint["bytes"] or 0) + int(writes["bytes"] or 0)
                candidates.append(
                    {
                        "thread_id": thread_id,
                        "last_activity_at": activity["last_activity_at"],
                        "inactive_days": max(
                            0,
                            int((_utc_now() - (_parse_iso(activity["last_activity_at"]) or _utc_now())).total_seconds() // 86400),
                        ),
                        "checkpoint_rows": checkpoint_rows,
                        "write_rows": write_rows,
                        "estimated_bytes": estimated_bytes,
                        "source": activity["source"],
                        "scheduled": False,
                        "running": self.manager.is_thread_running(thread_id),
                    }
                )
            return candidates

    async def _has_enabled_schedule(self, thread_id: str) -> bool:
        scheduler = getattr(self.manager, "scheduler_service", None)
        if scheduler is not None:
            tasks = await scheduler.store.list_tasks(
                thread_id=thread_id,
                include_disabled=False,
                limit=1,
            )
            return bool(tasks)
        return await asyncio.to_thread(self._has_enabled_schedule_sync, thread_id)

    def _has_enabled_schedule_sync(self, thread_id: str) -> bool:
        """Read persisted schedules even when the scheduler worker is disabled."""
        from app.config import settings

        path = Path(settings.scheduler_db_path)
        if not path.is_absolute():
            path = Path(settings.config_path).parent / path
        if not path.exists():
            return False
        try:
            with sqlite3.connect(str(path), timeout=5) as conn:
                conn.execute("PRAGMA busy_timeout=5000")
                return conn.execute(
                    """
                    SELECT 1 FROM scheduled_tasks
                    WHERE thread_id = ? AND enabled = 1
                    LIMIT 1
                    """,
                    (thread_id,),
                ).fetchone() is not None
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return False
            raise

    async def preview(self, *, limit: int = 100) -> dict[str, Any]:
        await asyncio.to_thread(self.backfill_activity_sync)
        limit = max(1, min(limit, 500))
        cutoff = _utc_now() - timedelta(days=self.config.inactive_days)
        candidates = await asyncio.to_thread(self._candidate_rows_sync, cutoff=cutoff, limit=limit)
        if self.config.protect_scheduled_threads:
            for candidate in candidates:
                candidate["scheduled"] = await self._has_enabled_schedule(candidate["thread_id"])
        eligible = [item for item in candidates if not item["running"] and not item["scheduled"]]
        return {
            "cutoff_at": _iso(cutoff),
            "candidates": candidates,
            "eligible_count": len(eligible),
            "estimated_reclaimable_bytes": sum(item["estimated_bytes"] for item in eligible),
        }

    def _latest_activity_sync(self) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(last_activity_at) AS last_activity_at FROM thread_activity"
            ).fetchone()
            return _parse_iso(row["last_activity_at"]) if row else None

    def _has_activity_since_sync(self, started_at: str) -> bool:
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM thread_activity WHERE last_activity_at > ? LIMIT 1",
                (started_at,),
            ).fetchone() is not None

    def _claim_thread_sync(
        self,
        thread_id: str,
        *,
        cutoff: datetime,
        job_id: str,
    ) -> bool:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                UPDATE thread_activity
                SET cleanup_claimed_by = ?, cleanup_claim_expires_at = ?, updated_at = ?
                WHERE thread_id = ?
                  AND protected = 0
                  AND last_activity_at < ?
                  AND (
                    cleanup_claimed_by IS NULL
                    OR cleanup_claim_expires_at IS NULL
                    OR cleanup_claim_expires_at <= ?
                  )
                """,
                (
                    job_id,
                    _iso(now + timedelta(seconds=_THREAD_CLAIM_SECONDS)),
                    _iso(now),
                    thread_id,
                    _iso(cutoff),
                    _iso(now),
                ),
            )
            return cur.rowcount == 1

    def _release_thread_claim_sync(self, thread_id: str, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE thread_activity
                SET cleanup_claimed_by = NULL, cleanup_claim_expires_at = NULL
                WHERE thread_id = ? AND cleanup_claimed_by = ?
                """,
                (thread_id, job_id),
            )

    def _renew_thread_claim_sync(self, thread_id: str, job_id: str) -> bool:
        now = _utc_now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE thread_activity
                SET cleanup_claim_expires_at = ?, updated_at = ?
                WHERE thread_id = ? AND cleanup_claimed_by = ?
                """,
                (
                    _iso(now + timedelta(seconds=_THREAD_CLAIM_SECONDS)),
                    _iso(now),
                    thread_id,
                    job_id,
                ),
            )
            return cur.rowcount == 1

    def _acquire_job_lease_sync(self, job_id: str) -> tuple[bool, str | None]:
        now = _utc_now()
        now_text = _iso(now)
        expires_at = _iso(now + timedelta(seconds=_JOB_LEASE_SECONDS))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner, expires_at FROM thread_cleanup_lease WHERE name = ?",
                (_LEASE_NAME,),
            ).fetchone()
            if row is not None and (_parse_iso(row["expires_at"]) or now) > now:
                return False, str(row["owner"])

            previous_owner = str(row["owner"]) if row is not None else None
            if previous_owner:
                conn.execute(
                    """
                    UPDATE thread_cleanup_runs
                    SET status = 'abandoned', completed_at = ?, current_thread_id = NULL,
                        error = COALESCE(error, 'Cleanup worker lease expired')
                    WHERE job_id = ? AND status IN ('pending', 'running')
                    """,
                    (now_text, previous_owner),
                )
                conn.execute(
                    """
                    UPDATE thread_activity
                    SET cleanup_claimed_by = NULL, cleanup_claim_expires_at = NULL
                    WHERE cleanup_claimed_by = ?
                    """,
                    (previous_owner,),
                )

            conn.execute(
                """
                INSERT INTO thread_cleanup_lease(name, owner, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    owner = excluded.owner,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (_LEASE_NAME, job_id, expires_at, now_text),
            )
            conn.execute(
                """
                UPDATE thread_activity
                SET cleanup_claimed_by = NULL, cleanup_claim_expires_at = NULL
                WHERE cleanup_claimed_by IS NOT NULL
                  AND cleanup_claim_expires_at IS NOT NULL
                  AND cleanup_claim_expires_at <= ?
                """,
                (now_text,),
            )
            return True, previous_owner

    def _renew_job_lease_sync(self, job_id: str) -> bool:
        now = _utc_now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE thread_cleanup_lease
                SET expires_at = ?, updated_at = ?
                WHERE name = ? AND owner = ?
                """,
                (
                    _iso(now + timedelta(seconds=_JOB_LEASE_SECONDS)),
                    _iso(now),
                    _LEASE_NAME,
                    job_id,
                ),
            )
            return cur.rowcount == 1

    def _release_job_lease_sync(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM thread_cleanup_lease WHERE name = ? AND owner = ?",
                (_LEASE_NAME, job_id),
            )

    def _run_by_id_sync(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM thread_cleanup_runs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            return dict(row) if row else None

    def _active_run_sync(self) -> dict[str, Any] | None:
        now_text = _iso()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.*, l.expires_at AS lease_expires_at
                FROM thread_cleanup_lease AS l
                LEFT JOIN thread_cleanup_runs AS r ON r.job_id = l.owner
                WHERE l.name = ? AND l.expires_at > ?
                LIMIT 1
                """,
                (_LEASE_NAME, now_text),
            ).fetchone()
            if row is not None and row["job_id"] is not None:
                return dict(row)
            lease = conn.execute(
                """
                SELECT owner, expires_at FROM thread_cleanup_lease
                WHERE name = ? AND expires_at > ?
                """,
                (_LEASE_NAME, now_text),
            ).fetchone()
            if lease is None:
                return None
            return {
                "job_id": lease["owner"],
                "status": "running",
                "lease_expires_at": lease["expires_at"],
            }

    def _insert_run_sync(self, status: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO thread_cleanup_runs (
                    job_id, trigger, dry_run, status, cutoff_at, started_at,
                    scanned, deleted, skipped, failed, estimated_reclaimed_bytes,
                    current_thread_id, deletion_limit, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    status["job_id"], status["trigger"], 1 if status["dry_run"] else 0,
                    status["status"], status["cutoff_at"], status["started_at"],
                    status["scanned"], status["deleted"], status["skipped"],
                    status["failed"], status["estimated_reclaimed_bytes"],
                    status.get("current_thread_id"), status["limit"], status.get("error"),
                ),
            )

    def _update_run_sync(self, status: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE thread_cleanup_runs
                SET status = ?, completed_at = ?, scanned = ?, deleted = ?,
                    skipped = ?, failed = ?, estimated_reclaimed_bytes = ?,
                    current_thread_id = ?, deletion_limit = ?, error = ?
                WHERE job_id = ? AND status != 'abandoned'
                """,
                (
                    status["status"], status.get("completed_at"), status["scanned"],
                    status["deleted"], status["skipped"], status["failed"],
                    status["estimated_reclaimed_bytes"], status.get("current_thread_id"),
                    status["limit"], status.get("error"), status["job_id"],
                ),
            )

    async def _lease_heartbeat(
        self,
        *,
        status: dict[str, Any],
        lost_lease: asyncio.Event,
    ) -> None:
        interval = max(1.0, _JOB_LEASE_SECONDS / 3)
        try:
            while True:
                await asyncio.sleep(interval)
                job_id = status["job_id"]
                if not await asyncio.to_thread(self._renew_job_lease_sync, job_id):
                    lost_lease.set()
                    return
                thread_id = status.get("current_thread_id")
                if thread_id:
                    # A missing claim can simply mean the deletion completed
                    # between reading current_thread_id and this heartbeat.
                    await asyncio.to_thread(
                        self._renew_thread_claim_sync,
                        thread_id,
                        job_id,
                    )
        except asyncio.CancelledError:
            raise

    async def start_run(
        self,
        *,
        trigger: str = "manual",
        dry_run: bool = False,
        limit: int | None = None,
        inactive_days: int | None = None,
    ) -> dict[str, Any]:
        # Keep the running check, durable run record and task creation atomic
        # from the perspective of concurrent API/scheduler callers. Without
        # this lock, two requests can both pass the check while the first one
        # is awaiting its SQLite insert.
        async with self._start_lock:
            if self._job_task is not None and not self._job_task.done():
                return {**(self._job_status or {}), "already_running": True}
            effective_limit = min(
                limit if limit is not None else self.config.max_deletions_per_run,
                self.config.max_deletions_per_run,
            )
            effective_limit = max(1, effective_limit)
            effective_inactive_days = inactive_days if inactive_days is not None else self.config.inactive_days
            effective_inactive_days = max(1, effective_inactive_days)
            job_id = f"cleanup-{uuid.uuid4().hex}"
            attempt_started_at = _utc_now()
            cutoff = attempt_started_at - timedelta(days=effective_inactive_days)

            acquired, current_owner = await asyncio.to_thread(
                self._acquire_job_lease_sync,
                job_id,
            )
            if not acquired:
                active = await asyncio.to_thread(
                    self._run_by_id_sync,
                    current_owner or "",
                )
                return {
                    **(active or {"job_id": current_owner, "status": "running"}),
                    "already_running": True,
                }

            if trigger == "scheduled" and self.config.quiet_period_minutes > 0:
                latest_activity = await asyncio.to_thread(self._latest_activity_sync)
                quiet_cutoff = attempt_started_at - timedelta(
                    minutes=self.config.quiet_period_minutes
                )
                if latest_activity is not None and latest_activity > quiet_cutoff:
                    retry_at = max(
                        attempt_started_at + timedelta(minutes=self.config.postpone_minutes),
                        latest_activity + timedelta(minutes=self.config.quiet_period_minutes),
                    )
                    await asyncio.to_thread(self._release_job_lease_sync, job_id)
                    return {
                        "status": "deferred",
                        "trigger": trigger,
                        "reason": "recent_activity",
                        "latest_activity_at": _iso(latest_activity),
                        "retry_at": _iso(retry_at),
                    }

            new_status: dict[str, Any] | None = None
            try:
                self._activity_event.clear()
                new_status = {
                    "job_id": job_id,
                    "trigger": trigger,
                    "dry_run": dry_run,
                    "status": "pending",
                    "cutoff_at": _iso(cutoff),
                    "started_at": _iso(attempt_started_at),
                    "completed_at": None,
                    "scanned": 0,
                    "deleted": 0,
                    "skipped": 0,
                    "failed": 0,
                    "estimated_reclaimed_bytes": 0,
                    "current_thread_id": None,
                    "limit": effective_limit,
                    "error": None,
                }
                self._job_status = new_status
                await asyncio.to_thread(self._insert_run_sync, self._job_status)
                self._job_task = asyncio.create_task(
                    self._execute_run(cutoff=cutoff, limit=effective_limit),
                    name=job_id,
                )
            except BaseException as exc:
                if new_status is not None:
                    new_status["status"] = "failed"
                    new_status["completed_at"] = _iso()
                    new_status["error"] = f"{type(exc).__name__}: {exc}"
                    try:
                        await asyncio.to_thread(self._update_run_sync, new_status)
                    except Exception:
                        logger.exception("Failed to persist cleanup startup failure")
                await asyncio.to_thread(self._release_job_lease_sync, job_id)
                self._job_task = None
                raise
            return dict(self._job_status)

    async def _execute_run(self, *, cutoff: datetime, limit: int) -> None:
        assert self._job_status is not None
        status = self._job_status
        status["status"] = "running"
        await asyncio.to_thread(self._update_run_sync, status)
        lost_lease = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._lease_heartbeat(status=status, lost_lease=lost_lease),
            name=f"{status['job_id']}-lease",
        )
        try:
            if status["trigger"] != "scheduled":
                await asyncio.to_thread(self.backfill_activity_sync)
            cursor: tuple[str, str] | None = None
            dry_run_eligible = 0
            batch_work = 0
            page_size = max(1, self.config.batch_size)
            exhausted = False
            while (
                dry_run_eligible if status["dry_run"] else status["deleted"]
            ) < limit and not exhausted:
                if self._stopping.is_set():
                    raise asyncio.CancelledError
                if lost_lease.is_set():
                    status["status"] = "lost_lease"
                    status["error"] = "Cleanup worker lost its database lease"
                    return
                if self.config.stop_on_new_activity and (
                    self._activity_event.is_set()
                    or await asyncio.to_thread(
                        self._has_activity_since_sync,
                        status["started_at"],
                    )
                ):
                    status["status"] = "stopped_on_activity"
                    return

                candidates = await asyncio.to_thread(
                    self._candidate_rows_sync,
                    cutoff=cutoff,
                    limit=page_size,
                    after=cursor,
                    include_storage_stats=bool(status["dry_run"]),
                )
                if not candidates:
                    exhausted = True
                    break
                last = candidates[-1]
                cursor = (last["last_activity_at"], last["thread_id"])
                status["scanned"] += len(candidates)

                for candidate in candidates:
                    if (
                        dry_run_eligible if status["dry_run"] else status["deleted"]
                    ) >= limit:
                        break
                    if self._stopping.is_set():
                        raise asyncio.CancelledError
                    if lost_lease.is_set():
                        status["status"] = "lost_lease"
                        status["error"] = "Cleanup worker lost its database lease"
                        return
                    if self.config.stop_on_new_activity and (
                        self._activity_event.is_set()
                        or await asyncio.to_thread(
                            self._has_activity_since_sync,
                            status["started_at"],
                        )
                    ):
                        status["status"] = "stopped_on_activity"
                        return

                    thread_id = candidate["thread_id"]
                    if self.manager.is_thread_running(thread_id):
                        status["skipped"] += 1
                        continue
                    if self.config.protect_scheduled_threads and await self._has_enabled_schedule(thread_id):
                        status["skipped"] += 1
                        continue

                    if status["dry_run"]:
                        dry_run_eligible += 1
                        status["skipped"] += 1
                        status["estimated_reclaimed_bytes"] += candidate["estimated_bytes"]
                        continue

                    claimed = await asyncio.to_thread(
                        self._claim_thread_sync,
                        thread_id,
                        cutoff=cutoff,
                        job_id=status["job_id"],
                    )
                    if not claimed:
                        status["skipped"] += 1
                        continue

                    status["current_thread_id"] = thread_id
                    batch_work += 1
                    await asyncio.to_thread(self._update_run_sync, status)
                    delete_task = asyncio.create_task(
                        asyncio.to_thread(self.manager.delete_thread_completely, thread_id)
                    )
                    cancelled = False
                    try:
                        result = await asyncio.shield(delete_task)
                    except asyncio.CancelledError:
                        cancelled = True
                        try:
                            result = await delete_task
                        except Exception as exc:
                            logger.warning(
                                "Thread deletion failed while cleanup was stopping (thread=%s)",
                                thread_id,
                                exc_info=True,
                            )
                            result = {"success": False, "detail": str(exc)}
                    except Exception as exc:
                        logger.warning(
                            "Thread deletion failed during cleanup (thread=%s)",
                            thread_id,
                            exc_info=True,
                        )
                        result = {"success": False, "detail": str(exc)}

                    if result.get("success"):
                        status["deleted"] += 1
                        status["estimated_reclaimed_bytes"] += candidate["estimated_bytes"]
                        await asyncio.to_thread(self.forget_thread_sync, thread_id)
                    else:
                        status["failed"] += 1
                        await asyncio.to_thread(
                            self._release_thread_claim_sync,
                            thread_id,
                            status["job_id"],
                        )
                    status["current_thread_id"] = None
                    if cancelled:
                        raise asyncio.CancelledError

                    if batch_work >= self.config.batch_size:
                        if not await asyncio.to_thread(
                            self._renew_job_lease_sync,
                            status["job_id"],
                        ):
                            lost_lease.set()
                        await asyncio.to_thread(self._update_run_sync, status)
                        batch_work = 0
                        interval = self.config.batch_interval_seconds
                        if interval > 0 and status["deleted"] < limit:
                            if self.config.stop_on_new_activity:
                                try:
                                    await asyncio.wait_for(
                                        self._activity_event.wait(),
                                        timeout=interval,
                                    )
                                except TimeoutError:
                                    pass
                            else:
                                await asyncio.sleep(interval)

                await asyncio.to_thread(self._update_run_sync, status)
            status["status"] = "completed" if status["failed"] == 0 else "completed_with_errors"
        except asyncio.CancelledError:
            status["status"] = "cancelled"
            raise
        except Exception as exc:
            status["status"] = "failed"
            status["error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("Thread cleanup job failed (job=%s)", status["job_id"])
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            status["current_thread_id"] = None
            status["completed_at"] = _iso()
            try:
                await asyncio.to_thread(self._update_run_sync, status)
            finally:
                await asyncio.to_thread(self._release_job_lease_sync, status["job_id"])
            if (
                status["status"] == "stopped_on_activity"
                and status["trigger"] == "scheduled"
                and self.config.enabled
            ):
                self._next_scheduled_at = _utc_now() + timedelta(
                    minutes=self.config.postpone_minutes
                )
                self._wake.set()
            logger.info(
                "Thread cleanup job finished (job=%s status=%s scanned=%d deleted=%d skipped=%d failed=%d)",
                status["job_id"], status["status"], status["scanned"], status["deleted"],
                status["skipped"], status["failed"],
            )

    def _next_run_at(self, now: datetime | None = None) -> datetime | None:
        if not self.config.enabled:
            return None
        now_utc = (now or _utc_now()).astimezone(UTC)
        tz = _timezone(self.config.timezone)
        local_now = now_utc.astimezone(tz)
        hour, minute = (int(piece) for piece in self.config.run_daily_at.split(":"))
        local_target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if local_target <= local_now:
            local_target += timedelta(days=1)
        return local_target.astimezone(UTC)

    def _last_run_sync(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM thread_cleanup_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def database_metrics_sync(self) -> dict[str, Any]:
        database_bytes = os.path.getsize(self.db_path) if self.db_path.exists() else 0
        with self._connect() as conn:
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            indexed_threads = int(conn.execute("SELECT COUNT(*) FROM thread_activity").fetchone()[0])
            protected_threads = int(conn.execute("SELECT COUNT(*) FROM thread_activity WHERE protected = 1").fetchone()[0])
            checkpoint_rows = write_rows = None
            row_counts_exact = database_bytes <= _EXACT_ROW_COUNT_MAX_BYTES
            if row_counts_exact:
                checkpoint_rows = int(conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0])
                write_rows = int(conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0])
        wal_path = Path(f"{self.db_path}-wal")
        return {
            "path": str(self.db_path),
            "database_bytes": database_bytes,
            "wal_bytes": os.path.getsize(wal_path) if wal_path.exists() else 0,
            "page_size": page_size,
            "page_count": page_count,
            "freelist_count": freelist_count,
            "reusable_bytes": page_size * freelist_count,
            "estimated_live_bytes": page_size * max(0, page_count - freelist_count),
            "indexed_threads": indexed_threads,
            "protected_threads": protected_threads,
            "checkpoint_rows": checkpoint_rows,
            "write_rows": write_rows,
            "row_counts_exact": row_counts_exact,
        }

    async def status(self) -> dict[str, Any]:
        last_run, active_run, metrics = await asyncio.gather(
            asyncio.to_thread(self._last_run_sync),
            asyncio.to_thread(self._active_run_sync),
            asyncio.to_thread(self.database_metrics_sync),
        )
        next_run = self._next_scheduled_at
        if self.config.enabled and next_run is None:
            next_run = self._next_run_at()
        return {
            "config": self.config.model_dump(),
            "running_job": active_run,
            "last_run": last_run,
            "next_run_at": _iso(next_run) if next_run else None,
            "database": metrics,
        }

    async def _run_loop(self) -> None:
        while not self._stopping.is_set():
            # Clear before doing maintenance so a reconfigure/stop signal that
            # arrives while work is in progress remains set for the wait below.
            self._wake.clear()
            try:
                if not self.config.enabled:
                    self._next_scheduled_at = None
                else:
                    await asyncio.to_thread(self.backfill_activity_sync)
                    if self._next_scheduled_at is None:
                        self._next_scheduled_at = self._next_run_at()
                if self._next_scheduled_at is not None and self._next_scheduled_at <= _utc_now():
                    result = await self.start_run(trigger="scheduled")
                    if result.get("status") == "deferred":
                        self._next_scheduled_at = _parse_iso(result.get("retry_at"))
                    elif result.get("already_running"):
                        self._next_scheduled_at = _utc_now() + timedelta(
                            minutes=self.config.postpone_minutes
                        )
                    else:
                        self._next_scheduled_at = self._next_run_at(
                            _utc_now() + timedelta(seconds=1)
                        )
                timeout = 300.0
                if self._next_scheduled_at is not None:
                    timeout = max(1.0, min(timeout, (self._next_scheduled_at - _utc_now()).total_seconds()))
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=timeout)
                except TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Thread cleanup maintenance loop failed")
                await asyncio.sleep(30)
