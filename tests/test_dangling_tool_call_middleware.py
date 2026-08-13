from langchain_core.messages import AIMessage, ToolMessage

from deerflow.agents.middlewares.dangling_tool_call_middleware import (
    DanglingToolCallMiddleware,
)


def test_raw_fallback_is_skipped_when_invalid_view_carries_same_call() -> None:
    message = AIMessage.model_construct(
        content="",
        type="ai",
        tool_calls=[],
        invalid_tool_calls=[
            {
                "id": "call_x",
                "name": "read_file",
                "args": None,
                "error": "invalid JSON",
            }
        ],
        additional_kwargs={
            "tool_calls": [
                {
                    "id": "call_x",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{bad"},
                }
            ]
        },
        response_metadata={},
    )

    patched = DanglingToolCallMiddleware()._build_patched_messages([message])

    assert patched is not None
    tool_messages = [item for item in patched if isinstance(item, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call_x"
    assert tool_messages[0].status == "error"
    assert "invalid JSON" in str(tool_messages[0].content)


def test_raw_fallback_is_still_used_without_structured_views() -> None:
    message = AIMessage.model_construct(
        content="",
        type="ai",
        tool_calls=[],
        invalid_tool_calls=[],
        additional_kwargs={
            "tool_calls": [
                {
                    "id": "call_raw",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ]
        },
        response_metadata={},
    )

    patched = DanglingToolCallMiddleware()._build_patched_messages([message])

    assert patched is not None
    tool_messages = [item for item in patched if isinstance(item, ToolMessage)]
    assert [item.tool_call_id for item in tool_messages] == ["call_raw"]
