from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain_core.tools import StructuredTool

from deerflow.subagents.config import SubagentConfig
from deerflow.subagents.executor import SubagentExecutor
from deerflow.tools.builtins.tool_search import (
    DeferredToolRegistry,
    clone_deferred_registry_for_tools,
    get_deferred_tools_prompt_section,
)


def _tool(name: str) -> StructuredTool:
    def noop() -> str:
        return "ok"

    return StructuredTool.from_function(noop, name=name, description=f"{name} description")


def test_clone_deferred_registry_for_tools_respects_filtered_tool_list() -> None:
    allowed = _tool("mcp_allowed")
    denied = _tool("mcp_denied")
    source = DeferredToolRegistry()
    source.register(allowed)
    source.register(denied)

    cloned = clone_deferred_registry_for_tools(source, [allowed])

    assert cloned is not None
    assert cloned.deferred_names == {"mcp_allowed"}


def test_deferred_tools_prompt_section_uses_registry_names() -> None:
    registry = DeferredToolRegistry()
    registry.register(_tool("mcp_calc"))

    section = get_deferred_tools_prompt_section(registry)

    assert section == "<available-deferred-tools>\nmcp_calc\n</available-deferred-tools>"


@pytest.mark.asyncio
async def test_subagent_exposes_skill_catalog_without_eager_body_injection() -> None:
    skill = SimpleNamespace(name="research", skill_file=SimpleNamespace(read_text=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must stay lazy"))))
    config = SubagentConfig(name="worker", description="Worker", system_prompt="Work carefully", skills=["research"])
    executor = SubagentExecutor(config=config, tools=[])

    with (
        patch("deerflow.skills.loader.load_skills", return_value=[skill]),
        patch("deerflow.agents.lead_agent.prompt.get_skills_prompt_section", return_value="<available_skills>research</available_skills>"),
    ):
        state = await executor._build_initial_state("Investigate this")

    assert state["messages"][0].content == "<available_skills>research</available_skills>"
    assert state["messages"][-1].content == "Investigate this"
