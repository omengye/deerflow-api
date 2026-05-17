"""Persistent scheduled task support for DeerFlow API."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone as fixed_timezone
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

ScheduleType = Literal["once", "interval", "daily"]

_scheduler_service: "SchedulerService | None" = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _dt_to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse_dt(value: str, timezone: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_get_tzinfo(timezone))
    return dt.astimezone(UTC)


def _get_tzinfo(timezone: str):
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        normalized = timezone.strip().upper()
        if normalized in {"UTC", "ETC/UTC", "Z"}:
            return UTC
        if timezone in {"Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin", "Asia/Urumqi"}:
            return fixed_timezone(timedelta(hours=8), "Asia/Shanghai")
        raise


def _parse_hhmm(value: str) -> time:
    parts = value.strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError("time_of_day must be HH:MM or HH:MM:SS")
    hour = int(parts[0])
    minute = int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    return time(hour=hour, minute=minute, second=second)


def compute_next_run_at(
    *,
    schedule_type: ScheduleType,
    schedule_expr: dict[str, Any],
    timezone: str,
    after: datetime | None = None,
) -> datetime | None:
    """Compute the next UTC run time for a supported schedule."""
    base = (after or _utc_now()).astimezone(UTC)

    if schedule_type == "once":
        run_at = schedule_expr.get("run_at")
        if not isinstance(run_at, str) or not run_at.strip():
            raise ValueError("schedule_expr.run_at is required for once schedules")
        dt = _parse_dt(run_at, timezone)
        return dt if dt > base else None

    if schedule_type == "interval":
        seconds = int(schedule_expr.get("every_seconds") or 0)
        if seconds < 1:
            raise ValueError("schedule_expr.every_seconds must be >= 1 for interval schedules")
        start_at_raw = schedule_expr.get("start_at")
        if isinstance(start_at_raw, str) and start_at_raw.strip():
            candidate = _parse_dt(start_at_raw, timezone)
        else:
            candidate = base + timedelta(seconds=seconds)
        while candidate <= base:
            candidate += timedelta(seconds=seconds)
        return candidate

    if schedule_type == "daily":
        tod_raw = schedule_expr.get("time_of_day")
        if not isinstance(tod_raw, str) or not tod_raw.strip():
            raise ValueError("schedule_expr.time_of_day is required for daily schedules")
        tz = _get_tzinfo(timezone)
        local_base = base.astimezone(tz)
        tod = _parse_hhmm(tod_raw)
        candidate = datetime.combine(local_base.date(), tod, tzinfo=tz)
        if candidate <= local_base:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    raise ValueError(f"Unsupported schedule_type: {schedule_type}")


@dataclass(frozen=True)
class ScheduledTask:
    id: str
    thread_id: str
    prompt: str
    schedule_type: ScheduleType
    schedule_expr: dict[str, Any]
    timezone: str
    enabled: bool
    next_run_at: str | None
    created_by: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any]
    kwargs: dict[str, Any]
    multitask_strategy: str


class SchedulerStore:
    """SQLite-backed scheduled task repository."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def setup(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    schedule_type TEXT NOT NULL,
                    schedule_expr TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    next_run_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    kwargs TEXT NOT NULL DEFAULT '{}',
                    multitask_strategy TEXT NOT NULL DEFAULT 'reject'
                );

                CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
                    ON scheduled_tasks(enabled, next_run_at);

                CREATE TABLE IF NOT EXISTS scheduled_task_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    run_id TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES scheduled_tasks(id)
                );

                CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_task
                    ON scheduled_task_runs(task_id, created_at DESC);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    async def create_task(
        self,
        *,
        thread_id: str,
        prompt: str,
        schedule_type: ScheduleType,
        schedule_expr: dict[str, Any],
        timezone: str,
        created_by: str = "agent",
        metadata: dict[str, Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        multitask_strategy: str = "reject",
    ) -> ScheduledTask:
        next_run_at = compute_next_run_at(
            schedule_type=schedule_type,
            schedule_expr=schedule_expr,
            timezone=timezone,
        )
        if next_run_at is None:
            raise ValueError("The schedule has no future run time")
        task_id = str(uuid.uuid4())
        now = _dt_to_iso(_utc_now())

        def _write() -> ScheduledTask:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO scheduled_tasks (
                        id, thread_id, prompt, schedule_type, schedule_expr,
                        timezone, enabled, next_run_at, created_by,
                        created_at, updated_at, metadata, kwargs, multitask_strategy
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        thread_id,
                        prompt,
                        schedule_type,
                        json.dumps(schedule_expr, ensure_ascii=False),
                        timezone,
                        _dt_to_iso(next_run_at),
                        created_by,
                        now,
                        now,
                        json.dumps(metadata or {}, ensure_ascii=False),
                        json.dumps(kwargs or {}, ensure_ascii=False),
                        multitask_strategy,
                    ),
                )
                row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
                return self._row_to_task(row)

        return await asyncio.to_thread(_write)

    async def list_tasks(
        self,
        *,
        thread_id: str | None = None,
        include_disabled: bool = False,
        limit: int = 50,
    ) -> list[ScheduledTask]:
        limit = max(1, min(limit, 200))

        def _read() -> list[ScheduledTask]:
            clauses = []
            params: list[Any] = []
            if thread_id:
                clauses.append("thread_id = ?")
                params.append(thread_id)
            if not include_disabled:
                clauses.append("enabled = 1")
            where = "WHERE " + " AND ".join(clauses) if clauses else ""
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT * FROM scheduled_tasks
                    {where}
                    ORDER BY next_run_at IS NULL, next_run_at ASC, created_at DESC
                    LIMIT ?
                    """,
                    (*params, limit),
                ).fetchall()
                return [self._row_to_task(row) for row in rows]

        return await asyncio.to_thread(_read)

    async def get_task(self, task_id: str) -> ScheduledTask | None:
        def _read() -> ScheduledTask | None:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
                return self._row_to_task(row) if row else None

        return await asyncio.to_thread(_read)

    async def set_enabled(self, task_id: str, enabled: bool) -> ScheduledTask | None:
        now = _dt_to_iso(_utc_now())

        def _write() -> ScheduledTask | None:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
                if row is None:
                    return None
                next_run_at = row["next_run_at"]
                if enabled and next_run_at is None:
                    task = self._row_to_task(row)
                    next_dt = compute_next_run_at(
                        schedule_type=task.schedule_type,
                        schedule_expr=task.schedule_expr,
                        timezone=task.timezone,
                    )
                    if next_dt is None:
                        raise ValueError("The schedule has no future run time")
                    next_run_at = _dt_to_iso(next_dt) if next_dt else None
                conn.execute(
                    "UPDATE scheduled_tasks SET enabled = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
                    (1 if enabled else 0, next_run_at, now, task_id),
                )
                updated = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
                return self._row_to_task(updated) if updated else None

        return await asyncio.to_thread(_write)

    async def delete_task(self, task_id: str) -> bool:
        def _write() -> bool:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
                return cur.rowcount > 0

        return await asyncio.to_thread(_write)

    async def claim_due_tasks(self, *, now: datetime, limit: int = 10) -> list[tuple[ScheduledTask, str, str]]:
        """Claim due tasks and create run records.

        Returns tuples of (task, task_run_id, scheduled_at).
        """
        now_iso = _dt_to_iso(now)

        def _claim() -> list[tuple[ScheduledTask, str, str]]:
            claimed: list[tuple[ScheduledTask, str, str]] = []
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    """
                    SELECT * FROM scheduled_tasks
                    WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                    ORDER BY next_run_at ASC
                    LIMIT ?
                    """,
                    (now_iso, limit),
                ).fetchall()
                for row in rows:
                    task = self._row_to_task(row)
                    scheduled_at = task.next_run_at or now_iso
                    run_row_id = str(uuid.uuid4())
                    next_dt = compute_next_run_at(
                        schedule_type=task.schedule_type,
                        schedule_expr=task.schedule_expr,
                        timezone=task.timezone,
                        after=now,
                    )
                    enabled = 1
                    if task.schedule_type == "once":
                        enabled = 0
                        next_run_at = None
                    else:
                        next_run_at = _dt_to_iso(next_dt) if next_dt else None
                        if next_run_at is None:
                            enabled = 0
                    conn.execute(
                        """
                        UPDATE scheduled_tasks
                        SET enabled = ?, next_run_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (enabled, next_run_at, now_iso, task.id),
                    )
                    conn.execute(
                        """
                        INSERT INTO scheduled_task_runs (
                            id, task_id, scheduled_at, status, created_at, updated_at
                        )
                        VALUES (?, ?, ?, 'claimed', ?, ?)
                        """,
                        (run_row_id, task.id, scheduled_at, now_iso, now_iso),
                    )
                    claimed.append((task, run_row_id, scheduled_at))
                conn.commit()
            return claimed

        return await asyncio.to_thread(_claim)

    async def mark_task_run_started(self, task_run_id: str, run_id: str) -> None:
        now = _dt_to_iso(_utc_now())

        def _write() -> None:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE scheduled_task_runs
                    SET run_id = ?, status = 'running', started_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (run_id, now, now, task_run_id),
                )

        await asyncio.to_thread(_write)

    async def mark_task_run_finished(self, task_run_id: str, status: str, error: str | None = None) -> None:
        now = _dt_to_iso(_utc_now())

        def _write() -> None:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE scheduled_task_runs
                    SET status = ?, error = ?, finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, error, now, now, task_run_id),
                )

        await asyncio.to_thread(_write)

    async def list_task_runs(self, task_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))

        def _read() -> list[dict[str, Any]]:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM scheduled_task_runs
                    WHERE task_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (task_id, limit),
                ).fetchall()
                return [dict(row) for row in rows]

        return await asyncio.to_thread(_read)

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> ScheduledTask:
        return ScheduledTask(
            id=row["id"],
            thread_id=row["thread_id"],
            prompt=row["prompt"],
            schedule_type=row["schedule_type"],
            schedule_expr=json.loads(row["schedule_expr"] or "{}"),
            timezone=row["timezone"],
            enabled=bool(row["enabled"]),
            next_run_at=row["next_run_at"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=json.loads(row["metadata"] or "{}"),
            kwargs=json.loads(row["kwargs"] or "{}"),
            multitask_strategy=row["multitask_strategy"],
        )


class SchedulerService:
    """Single-process scheduler loop that dispatches persisted tasks."""

    def __init__(
        self,
        *,
        store: SchedulerStore,
        manager: Any,
        poll_interval_seconds: float,
        default_timezone: str = "Asia/Shanghai",
    ) -> None:
        self.store = store
        self.manager = manager
        self.poll_interval_seconds = max(0.5, poll_interval_seconds)
        self.default_timezone = default_timezone
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self.store.setup()
        set_scheduler_service(self)
        self._stopping.clear()
        self._task = asyncio.create_task(self._run_loop(), name="deerflow-scheduler")
        logger.info("Scheduler service started")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        set_scheduler_service(None)
        logger.info("Scheduler service stopped")

    async def _run_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler tick failed")
            await asyncio.sleep(self.poll_interval_seconds)

    async def tick(self) -> None:
        claimed = await self.store.claim_due_tasks(now=_utc_now())
        for task, task_run_id, scheduled_at in claimed:
            asyncio.create_task(self._dispatch(task, task_run_id, scheduled_at))

    async def _dispatch(self, task: ScheduledTask, task_run_id: str, scheduled_at: str) -> None:
        try:
            record = await self.manager.start_client_stream_run(
                thread_id=task.thread_id,
                message=task.prompt,
                kwargs=task.kwargs,
                entrypoint="scheduled_task",
                on_disconnect="continue",
                multitask_strategy=task.multitask_strategy,
            )
            await self.store.mark_task_run_started(task_run_id, record.run_id)
            delivery_task = self._start_delivery(task, record.run_id)
            if record.task is not None:
                try:
                    await record.task
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Scheduled run task raised unexpectedly", exc_info=True)
            delivery_error: str | None = None
            if delivery_task is not None:
                try:
                    await delivery_task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    delivery_error = f"Delivery failed: {exc}"
                    logger.warning("Scheduled task %s delivery failed", task.id, exc_info=True)
            current = self.manager.run_manager.get(record.run_id)
            status = current.status.value if current is not None else "unknown"
            error = current.error if current is not None else None
            if delivery_error:
                status = "error"
                error = delivery_error
            await self.store.mark_task_run_finished(task_run_id, status, error)
            logger.info("Scheduled task %s fired at %s as run %s", task.id, scheduled_at, record.run_id)
        except asyncio.CancelledError:
            await self.store.mark_task_run_finished(task_run_id, "cancelled", "Scheduler shutdown")
            raise
        except Exception as exc:
            logger.exception("Scheduled task %s dispatch failed", task.id)
            await self.store.mark_task_run_finished(task_run_id, "error", str(exc))

    def _start_delivery(self, task: ScheduledTask, run_id: str) -> asyncio.Task | None:
        delivery = task.metadata.get("delivery")
        if not isinstance(delivery, dict) or delivery.get("channel") != "feishu":
            return None

        chat_id = delivery.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id:
            logger.warning("Scheduled task %s has invalid Feishu delivery metadata", task.id)
            return None

        feishu_channel = getattr(self.manager, "feishu_channel", None)
        if feishu_channel is None:
            logger.warning("Scheduled task %s requested Feishu delivery but channel is unavailable", task.id)
            return None

        return asyncio.create_task(
            feishu_channel.render_run_to_chat(
                run_id=run_id,
                thread_id=task.thread_id,
                chat_id=chat_id,
            ),
            name=f"scheduled-feishu-delivery-{task.id}",
        )


def set_scheduler_service(service: SchedulerService | None) -> None:
    global _scheduler_service
    _scheduler_service = service


def get_scheduler_service() -> SchedulerService:
    if _scheduler_service is None:
        raise RuntimeError("Scheduler service is not available")
    return _scheduler_service


def task_to_dict(task: ScheduledTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "thread_id": task.thread_id,
        "prompt": task.prompt,
        "schedule_type": task.schedule_type,
        "schedule_expr": task.schedule_expr,
        "timezone": task.timezone,
        "enabled": task.enabled,
        "next_run_at": task.next_run_at,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "metadata": task.metadata,
        "kwargs": task.kwargs,
        "multitask_strategy": task.multitask_strategy,
    }
