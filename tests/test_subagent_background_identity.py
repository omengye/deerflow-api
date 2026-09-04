from types import SimpleNamespace
import threading
import time

import deerflow.subagents.executor as executor_module
import pytest
from langchain_core.messages import AIMessage

from deerflow.agents.middlewares.loop_detection_middleware import (
    AGENT_TERMINATION_KEY,
    TOOL_CALL_LIMIT_STOP_REASON,
)
from deerflow.subagents.config import SubagentConfig
from deerflow.subagents.executor import (
    SubagentExecutor,
    SubagentResult,
    SubagentStatus,
    finalize_cancelled_background_task,
)


def test_subagent_lifecycle_timestamps_are_utc_aware() -> None:
    result = SubagentResult(
        task_id="utc",
        trace_id="trace-1",
        status=SubagentStatus.PENDING,
    )

    assert result.try_mark_running() is True
    assert result.started_at is not None
    assert result.started_at.utcoffset() is not None
    assert result.started_at.utcoffset().total_seconds() == 0

    assert result.try_set_terminal(SubagentStatus.COMPLETED, result="done") is True
    assert result.completed_at is not None
    assert result.completed_at.utcoffset() is not None
    assert result.completed_at.utcoffset().total_seconds() == 0


class _DeferredPool:
    def __init__(self) -> None:
        self.calls = []

    def submit(self, func, *args, **kwargs):
        self.calls.append((func, args, kwargs))
        return SimpleNamespace()


class _FailingPool:
    def submit(self, func, *args, **kwargs):
        raise RuntimeError("scheduler is shut down")


def _executor() -> SubagentExecutor:
    instance = object.__new__(SubagentExecutor)
    instance.trace_id = "trace-1"
    instance.config = SimpleNamespace(name="worker", timeout_seconds=60)
    return instance


def test_reused_external_tool_call_ids_get_distinct_registry_keys(monkeypatch) -> None:
    scheduler = _DeferredPool()
    monkeypatch.setattr(executor_module, "_scheduler_pool", scheduler)

    first_id = _executor().execute_async("first", task_id="provider-call-1")
    second_id = _executor().execute_async("second", task_id="provider-call-1")

    try:
        assert first_id != second_id
        first = executor_module.get_background_task_result(first_id)
        second = executor_module.get_background_task_result(second_id)
        assert first is not None and second is not None
        assert first is not second
        assert first.task_id == first_id
        assert second.task_id == second_id
        assert first.external_task_id == second.external_task_id == "provider-call-1"
    finally:
        with executor_module._background_tasks_lock:
            executor_module._background_tasks.pop(first_id, None)
            executor_module._background_tasks.pop(second_id, None)


def test_scheduler_submission_failure_removes_unreachable_pending_result(monkeypatch) -> None:
    monkeypatch.setattr(executor_module, "_scheduler_pool", _FailingPool())
    existing_ids = set(executor_module._background_tasks)

    with pytest.raises(RuntimeError, match="scheduler is shut down"):
        _executor().execute_async("work")

    assert set(executor_module._background_tasks) == existing_ids


def test_terminal_outcome_is_first_writer_wins() -> None:
    result = SubagentResult(
        task_id="execution-1",
        external_task_id="provider-call-1",
        trace_id="trace-1",
        status=SubagentStatus.RUNNING,
    )

    assert result.try_set_terminal(SubagentStatus.TIMED_OUT, error="deadline") is True
    assert result.try_set_terminal(SubagentStatus.COMPLETED, result="late answer") is False
    assert result.status == SubagentStatus.TIMED_OUT
    assert result.error == "deadline"
    assert result.result is None


def test_terminal_result_freezes_ai_messages() -> None:
    original_messages = [{"id": "first"}]
    result = SubagentResult(
        task_id="execution-1",
        trace_id="trace-1",
        status=SubagentStatus.RUNNING,
        ai_messages=original_messages,
    )

    assert result.try_set_terminal(SubagentStatus.TIMED_OUT, error="deadline") is True
    original_messages.append({"id": "late"})

    assert result.ai_messages == [{"id": "first"}]


@pytest.mark.asyncio
async def test_subagent_tool_limit_is_not_published_as_completed(monkeypatch) -> None:
    limit_message = "Tool-call safety limit reached"

    class FakeAgent:
        async def astream(self, *_args, context, **_kwargs):
            context["stop_reason"] = TOOL_CALL_LIMIT_STOP_REASON
            yield (
                "values",
                {
                    "messages": [
                        AIMessage(
                            content="partial work",
                            additional_kwargs={
                                AGENT_TERMINATION_KEY: {
                                    "reason": TOOL_CALL_LIMIT_STOP_REASON,
                                    "incomplete": True,
                                    "message": limit_message,
                                }
                            },
                        )
                    ]
                },
            )

    executor = SubagentExecutor(
        SubagentConfig(
            name="worker",
            description="worker",
            system_prompt="work",
        ),
        tools=[],
    )

    async def initial_state(_task: str) -> dict[str, list[object]]:
        return {"messages": []}

    async def close_model(_model: object) -> None:
        return None

    monkeypatch.setattr(
        executor,
        "_create_agent",
        lambda stream_callback=None: (FakeAgent(), object()),
    )
    monkeypatch.setattr(executor, "_build_initial_state", initial_state)
    monkeypatch.setattr(executor_module, "aclose_chat_model", close_model)

    result = await executor._aexecute("finish the task")

    assert result.status == SubagentStatus.LIMIT_REACHED
    assert result.result == "partial work"
    assert result.error == limit_message
    assert result.termination_reason == TOOL_CALL_LIMIT_STOP_REASON


def test_sync_bootstrap_failure_records_error(monkeypatch) -> None:
    instance = _executor()

    async def fail_before_result(*args, **kwargs):
        raise RuntimeError("bootstrap failed")

    monkeypatch.setattr(instance, "_aexecute", fail_before_result)

    result = instance.execute("work")

    assert result.status == SubagentStatus.FAILED
    assert result.error == "bootstrap failed"
    assert result.completed_at is not None


def test_owner_side_timeout_fences_late_worker_and_allows_cleanup() -> None:
    result = SubagentResult(
        task_id="execution-timeout",
        trace_id="trace-1",
        status=SubagentStatus.RUNNING,
    )
    with executor_module._background_tasks_lock:
        executor_module._background_tasks[result.task_id] = result

    try:
        assert finalize_cancelled_background_task(
            result.task_id,
            status=SubagentStatus.TIMED_OUT,
            error="polling timeout",
        )
        assert result.cancel_event.is_set()
        assert result.status == SubagentStatus.TIMED_OUT
        assert result.try_set_terminal(
            SubagentStatus.COMPLETED,
            result="late worker answer",
        ) is False

        executor_module.cleanup_background_task(result.task_id)
        assert executor_module.get_background_task_result(result.task_id) is None
    finally:
        with executor_module._background_tasks_lock:
            executor_module._background_tasks.pop(result.task_id, None)


def test_timed_out_workers_do_not_starve_later_execution(monkeypatch) -> None:
    scheduler = _DeferredPool()
    monkeypatch.setattr(executor_module, "_scheduler_pool", scheduler)
    monkeypatch.setattr(executor_module, "MAX_QUARANTINED_SUBAGENTS", 4)
    with executor_module._execution_threads_lock:
        executor_module._active_execution_threads.clear()
        executor_module._quarantined_execution_threads.clear()

    blocker = threading.Event()

    def blocked_execute(task, result, callback):
        blocker.wait(5)
        result.try_set_terminal(SubagentStatus.COMPLETED, result=task)
        return result

    blocked = _executor()
    blocked.config.timeout_seconds = 0.01
    monkeypatch.setattr(blocked, "execute", blocked_execute)
    blocked_ids = [blocked.execute_async(f"blocked-{index}") for index in range(3)]
    for run_task, args, kwargs in list(scheduler.calls):
        run_task(*args, **kwargs)
    scheduler.calls.clear()

    fast = _executor()
    fast.config.timeout_seconds = 1

    def fast_execute(task, result, callback):
        result.try_set_terminal(SubagentStatus.COMPLETED, result="fast")
        return result

    monkeypatch.setattr(fast, "execute", fast_execute)
    fast_id = fast.execute_async("fast")
    run_task, args, kwargs = scheduler.calls.pop()
    run_task(*args, **kwargs)

    try:
        assert all(
            executor_module.get_background_task_result(task_id).status
            == SubagentStatus.TIMED_OUT
            for task_id in blocked_ids
        )
        assert executor_module.get_background_task_result(fast_id).status == SubagentStatus.COMPLETED
    finally:
        blocker.set()
        for task_id in [*blocked_ids, fast_id]:
            with executor_module._background_tasks_lock:
                executor_module._background_tasks.pop(task_id, None)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with executor_module._execution_threads_lock:
                if not executor_module._quarantined_execution_threads:
                    break
            time.sleep(0.01)


def test_quarantine_cap_refuses_unbounded_detached_workers(monkeypatch) -> None:
    scheduler = _DeferredPool()
    monkeypatch.setattr(executor_module, "_scheduler_pool", scheduler)
    monkeypatch.setattr(executor_module, "MAX_QUARANTINED_SUBAGENTS", 1)
    with executor_module._execution_threads_lock:
        executor_module._active_execution_threads.clear()
        executor_module._quarantined_execution_threads.clear()

    blocker = threading.Event()
    instance = _executor()
    instance.config.timeout_seconds = 0.01

    def blocked_execute(task, result, callback):
        blocker.wait(5)
        return result

    monkeypatch.setattr(instance, "execute", blocked_execute)
    first_id = instance.execute_async("first")
    run_task, args, kwargs = scheduler.calls.pop(0)
    run_task(*args, **kwargs)
    second_id = instance.execute_async("second")
    run_task, args, kwargs = scheduler.calls.pop(0)
    run_task(*args, **kwargs)

    try:
        first = executor_module.get_background_task_result(first_id)
        second = executor_module.get_background_task_result(second_id)
        assert first is not None and first.status == SubagentStatus.TIMED_OUT
        assert second is not None and second.status == SubagentStatus.FAILED
        assert "capacity is exhausted" in (second.error or "")
    finally:
        blocker.set()
        for task_id in (first_id, second_id):
            with executor_module._background_tasks_lock:
                executor_module._background_tasks.pop(task_id, None)


def test_owner_cancellation_releases_scheduler_wait_promptly(monkeypatch) -> None:
    blocker = threading.Event()
    result = SubagentResult(
        task_id="cancelled-execution",
        trace_id="trace-1",
        status=SubagentStatus.RUNNING,
    )

    def blocked() -> SubagentResult:
        blocker.wait(5)
        return result

    started = executor_module._start_isolated_execution(
        blocked,
        execution_id=result.task_id,
    )
    assert started is not None
    _, worker = started
    result.try_set_terminal(SubagentStatus.CANCELLED, error="parent cancelled")
    result.cancel_event.set()

    try:
        assert executor_module._quarantine_execution_thread(worker)
        with executor_module._execution_threads_lock:
            assert worker not in executor_module._active_execution_threads
            assert worker in executor_module._quarantined_execution_threads
    finally:
        blocker.set()
