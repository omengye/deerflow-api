"""Built-in tools for agent-managed scheduled tasks."""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain.tools import ToolRuntime, tool
from langgraph.config import get_config

from deerflow.agents.thread_state import AgentContext, ThreadState
from deerflow.runtime.scheduler import get_scheduler_service, task_to_dict

ScheduleType = Literal["once", "interval", "daily"]


def _get_thread_id(runtime: ToolRuntime[AgentContext, ThreadState] | None) -> str | None:
    if runtime is not None:
        if runtime.context and runtime.context.get("thread_id"):
            return runtime.context.get("thread_id")
        thread_id = runtime.config.get("configurable", {}).get("thread_id")
        if thread_id:
            return thread_id
    try:
        return get_config().get("configurable", {}).get("thread_id")
    except Exception:
        return None


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@tool("create_scheduled_task", parse_docstring=True)
async def create_scheduled_task_tool(
    runtime: ToolRuntime[AgentContext, ThreadState],
    prompt: str,
    schedule_type: ScheduleType,
    run_at: str | None = None,
    every_seconds: int | None = None,
    time_of_day: str | None = None,
    timezone: str | None = None,
    thread_id: str | None = None,
    multitask_strategy: Literal["reject", "interrupt", "rollback"] = "reject",
) -> str:
    """Create a persistent scheduled task for this DeerFlow service.

    Use this only after the user has confirmed the exact schedule and task
    content. The task survives API service restarts because it is stored in
    SQLite.

    Args:
        prompt: The user-facing instruction to run when the task fires.
        schedule_type: once, interval, or daily.
        run_at: ISO-8601 datetime for once schedules, e.g. 2026-05-18T09:00:00+08:00.
        every_seconds: Interval length in seconds for interval schedules.
        time_of_day: Local time for daily schedules, HH:MM or HH:MM:SS.
        timezone: IANA timezone, e.g. Asia/Shanghai. Defaults to service timezone.
        thread_id: Optional target thread. Defaults to the current conversation thread.
        multitask_strategy: What to do if the target thread already has a run.
    """
    target_thread_id = thread_id or _get_thread_id(runtime)
    if not target_thread_id:
        raise ValueError("thread_id is required when no current conversation thread is available")

    schedule_expr: dict[str, Any]
    if schedule_type == "once":
        if not run_at:
            raise ValueError("run_at is required for once schedules")
        schedule_expr = {"run_at": run_at}
    elif schedule_type == "interval":
        if not every_seconds:
            raise ValueError("every_seconds is required for interval schedules")
        schedule_expr = {"every_seconds": every_seconds}
        if run_at:
            schedule_expr["start_at"] = run_at
    elif schedule_type == "daily":
        if not time_of_day:
            raise ValueError("time_of_day is required for daily schedules")
        schedule_expr = {"time_of_day": time_of_day}
    else:
        raise ValueError(f"Unsupported schedule_type: {schedule_type}")

    service = get_scheduler_service()
    task = await service.store.create_task(
        thread_id=target_thread_id,
        prompt=prompt,
        schedule_type=schedule_type,
        schedule_expr=schedule_expr,
        timezone=timezone or service.default_timezone,
        created_by="agent",
        multitask_strategy=multitask_strategy,
    )
    return _json({"created": task_to_dict(task)})


@tool("list_scheduled_tasks", parse_docstring=True)
async def list_scheduled_tasks_tool(
    runtime: ToolRuntime[AgentContext, ThreadState],
    include_disabled: bool = False,
    all_threads: bool = False,
    limit: int = 50,
) -> str:
    """List persistent scheduled tasks.

    Args:
        include_disabled: Include disabled or completed one-time tasks.
        all_threads: If true, list tasks across all threads. Otherwise list only this conversation thread.
        limit: Maximum number of tasks to return.
    """
    thread_id = None if all_threads else _get_thread_id(runtime)
    tasks = await get_scheduler_service().store.list_tasks(
        thread_id=thread_id,
        include_disabled=include_disabled,
        limit=limit,
    )
    return _json({"tasks": [task_to_dict(task) for task in tasks]})


@tool("set_scheduled_task_enabled", parse_docstring=True)
async def set_scheduled_task_enabled_tool(
    task_id: str,
    enabled: bool,
) -> str:
    """Enable or disable a persistent scheduled task.

    Args:
        task_id: The scheduled task ID.
        enabled: True to enable the task, false to pause it.
    """
    task = await get_scheduler_service().store.set_enabled(task_id, enabled)
    if task is None:
        raise ValueError(f"Scheduled task not found: {task_id}")
    return _json({"updated": task_to_dict(task)})


@tool("delete_scheduled_task", parse_docstring=True)
async def delete_scheduled_task_tool(task_id: str) -> str:
    """Delete a persistent scheduled task.

    Args:
        task_id: The scheduled task ID to delete.
    """
    deleted = await get_scheduler_service().store.delete_task(task_id)
    if not deleted:
        raise ValueError(f"Scheduled task not found: {task_id}")
    return _json({"deleted": task_id})


@tool("list_scheduled_task_runs", parse_docstring=True)
async def list_scheduled_task_runs_tool(task_id: str, limit: int = 20) -> str:
    """List execution history for a scheduled task.

    Args:
        task_id: The scheduled task ID.
        limit: Maximum number of execution records to return.
    """
    runs = await get_scheduler_service().store.list_task_runs(task_id, limit=limit)
    return _json({"task_id": task_id, "runs": runs})


scheduled_task_tools = [
    create_scheduled_task_tool,
    list_scheduled_tasks_tool,
    set_scheduled_task_enabled_tool,
    delete_scheduled_task_tool,
    list_scheduled_task_runs_tool,
]
