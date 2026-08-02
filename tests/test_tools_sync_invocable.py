"""Tests for adding sync wrappers only to async StructuredTool instances."""

from langchain_core.tools import StructuredTool

from deerflow.tools.tools import _ensure_sync_invocable_tool


async def _async_echo(value: str) -> str:
    return value


def test_async_structured_tool_gets_sync_wrapper() -> None:
    tool = StructuredTool.from_function(
        coroutine=_async_echo,
        name="async_echo",
        description="Return the provided value.",
    )
    assert tool.func is None

    resolved = _ensure_sync_invocable_tool(tool)

    assert resolved is tool
    assert tool.func is not None
    assert tool.invoke({"value": "hello"}) == "hello"
