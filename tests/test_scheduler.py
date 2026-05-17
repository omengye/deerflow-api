from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from deerflow.runtime.scheduler import SchedulerService, SchedulerStore, compute_next_run_at
from deerflow.runtime import RunStatus
from deerflow.tools.builtins.scheduled_task_tools import _delivery_metadata_for_thread


def test_compute_next_run_at_daily_uses_timezone():
    next_run = compute_next_run_at(
        schedule_type="daily",
        schedule_expr={"time_of_day": "09:00"},
        timezone="Asia/Shanghai",
        after=datetime(2026, 5, 17, 0, 30, tzinfo=UTC),
    )

    assert next_run == datetime(2026, 5, 17, 1, 0, tzinfo=UTC)


def test_delivery_metadata_for_feishu_thread():
    assert _delivery_metadata_for_thread("feishu_chat-1") == {
        "delivery": {"channel": "feishu", "chat_id": "chat-1"}
    }
    assert _delivery_metadata_for_thread("thread-1") == {}


@pytest.mark.asyncio
async def test_scheduler_store_persists_tasks(tmp_path):
    db_path = tmp_path / "scheduled_tasks.db"
    store = SchedulerStore(db_path)
    store.setup()

    task = await store.create_task(
        thread_id="thread-1",
        prompt="run report",
        schedule_type="daily",
        schedule_expr={"time_of_day": "09:00"},
        timezone="Asia/Shanghai",
    )

    reloaded = SchedulerStore(db_path)
    reloaded.setup()
    tasks = await reloaded.list_tasks(thread_id="thread-1")

    assert len(tasks) == 1
    assert tasks[0].id == task.id
    assert tasks[0].prompt == "run report"
    assert tasks[0].enabled is True


@pytest.mark.asyncio
async def test_scheduler_service_dispatches_due_task(tmp_path):
    db_path = tmp_path / "scheduled_tasks.db"
    store = SchedulerStore(db_path)
    store.setup()
    run_task = asyncio.create_task(asyncio.sleep(0))
    fake_record = SimpleNamespace(
        run_id="run-1",
        task=run_task,
    )
    calls = []

    class FakeRunManager:
        def get(self, run_id):
            return SimpleNamespace(status=RunStatus.success, error=None)

    class FakeManager:
        run_manager = FakeRunManager()

        async def start_client_stream_run(self, **kwargs):
            calls.append(kwargs)
            return fake_record

    await store.create_task(
        thread_id="thread-1",
        prompt="run due task",
        schedule_type="once",
        schedule_expr={"run_at": (datetime.now(UTC) + timedelta(milliseconds=10)).isoformat()},
        timezone="UTC",
    )

    await asyncio.sleep(0.02)
    service = SchedulerService(store=store, manager=FakeManager(), poll_interval_seconds=1)
    await service.tick()
    await asyncio.sleep(0)

    assert calls
    assert calls[0]["thread_id"] == "thread-1"
    assert calls[0]["message"] == "run due task"
    runs = await store.list_task_runs((await store.list_tasks(include_disabled=True))[0].id)
    assert runs[0]["run_id"] == "run-1"


@pytest.mark.asyncio
async def test_scheduler_service_starts_feishu_delivery(tmp_path):
    db_path = tmp_path / "scheduled_tasks.db"
    store = SchedulerStore(db_path)
    store.setup()
    delivery_calls = []

    class FakeFeishuChannel:
        async def render_run_to_chat(self, **kwargs):
            delivery_calls.append(kwargs)

    class FakeManager:
        feishu_channel = FakeFeishuChannel()

    task = await store.create_task(
        thread_id="feishu_chat-1",
        prompt="run due task",
        schedule_type="once",
        schedule_expr={"run_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat()},
        timezone="UTC",
        metadata={"delivery": {"channel": "feishu", "chat_id": "chat-1"}},
    )

    service = SchedulerService(store=store, manager=FakeManager(), poll_interval_seconds=1)
    delivery_task = service._start_delivery(task, "run-1")
    assert delivery_task is not None
    await delivery_task

    assert delivery_calls == [
        {
            "run_id": "run-1",
            "thread_id": "feishu_chat-1",
            "chat_id": "chat-1",
        }
    ]
