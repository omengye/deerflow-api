from langchain_core.tools import StructuredTool

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
