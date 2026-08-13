from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest

import deerflow.tools as tools_module
from deerflow.subagents.config import SubagentConfig
from deerflow.subagents.executor import SubagentStatus

task_tool_module = importlib.import_module("deerflow.tools.builtins.task_tool")
tool_search_module = importlib.import_module("deerflow.tools.builtins.tool_search")


@pytest.mark.asyncio
async def test_task_tool_propagates_acp_policy_to_internal_subagent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    permission_middleware = object()
    captured: dict[str, Any] = {}
    cleanup_calls: list[str] = []
    lookup_calls: list[str] = []
    available_tool_calls: list[dict[str, Any]] = []
    child_config = SubagentConfig(
        name="general-purpose",
        description="General task worker",
        system_prompt="Child base prompt",
        skills=["research", "child-only"],
        timeout_seconds=10,
    )

    def fake_get_available_tools(**kwargs: Any) -> list[Any]:
        available_tool_calls.append(kwargs)
        return [
            SimpleNamespace(name="read_file"),
            SimpleNamespace(name="write_file"),
            SimpleNamespace(name="task"),
            SimpleNamespace(name="invoke_acp_agent"),
            SimpleNamespace(name="create_scheduled_task"),
            SimpleNamespace(name="not_allowlisted"),
        ]

    class FakeExecutor:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def execute_async(self, prompt: str, **kwargs: Any) -> str:
            captured["prompt"] = prompt
            captured["execute_kwargs"] = kwargs
            return "execution-1"

    completed_result = SimpleNamespace(
        status=SubagentStatus.COMPLETED,
        result="finished",
        error=None,
        ai_messages=[],
    )

    monkeypatch.setattr(task_tool_module, "get_available_subagent_names", lambda: ["general-purpose"])
    monkeypatch.setattr(task_tool_module, "get_subagent_config", lambda _name: child_config)
    monkeypatch.setattr(task_tool_module, "SubagentExecutor", FakeExecutor)
    def get_result(task_id: str):
        lookup_calls.append(task_id)
        return completed_result

    monkeypatch.setattr(task_tool_module, "get_background_task_result", get_result)
    monkeypatch.setattr(task_tool_module, "cleanup_background_task", cleanup_calls.append)
    monkeypatch.setattr(tools_module, "get_available_tools", fake_get_available_tools)
    monkeypatch.setattr(tool_search_module, "get_deferred_registry", lambda: None)
    monkeypatch.setattr(tool_search_module, "clone_deferred_registry_for_tools", lambda _registry, _tools: None)

    runtime = SimpleNamespace(
        state={"sandbox": "sandbox-state", "thread_data": {"source": "parent"}},
        context={"thread_id": "session-1"},
        config={
            "configurable": {"thread_id": "session-1"},
            "metadata": {
                "model_name": "parent-model",
                "thinking_enabled": True,
                "trace_id": "trace-1",
                "tool_groups": ["file:read", "file:write"],
                "available_skills": ["research", "shared"],
                "subagent_system_prompt_overlay": "ACP child safety overlay",
                "subagent_excluded_tool_names": [
                    "invoke_acp_agent",
                    "create_scheduled_task",
                ],
                "subagent_allowed_tool_names": [
                    "read_file",
                    "write_file",
                    "task",
                    "invoke_acp_agent",
                    "create_scheduled_task",
                ],
                "subagent_middlewares": [permission_middleware],
            },
        },
    )

    result = await task_tool_module._task_tool_impl(
        runtime=runtime,
        description="Run child task",
        prompt="Inspect the workspace",
        subagent_type="general-purpose",
        tool_call_id="tool-call-1",
    )

    assert result == "Task Succeeded. Result: finished"
    assert available_tool_calls == [
        {
            "model_name": "parent-model",
            "groups": ["file:read", "file:write"],
            "subagent_enabled": False,
        }
    ]
    assert [tool.name for tool in captured["tools"]] == ["read_file", "write_file"]
    assert captured["config"].skills == ["research"]
    assert captured["config"].system_prompt.endswith("ACP child safety overlay")
    assert captured["middlewares"] == [permission_middleware]
    assert captured["parent_model"] == "parent-model"
    assert captured["thinking_enabled"] is True
    assert captured["prompt"] == "Inspect the workspace"
    assert lookup_calls == ["execution-1"]
    assert cleanup_calls == ["execution-1"]
