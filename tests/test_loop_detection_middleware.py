from types import SimpleNamespace

from langchain_core.messages import AIMessage

from deerflow.agents.middlewares.loop_detection_middleware import LoopDetectionMiddleware


def _runtime(**context):
    return SimpleNamespace(context=context)


def _state(tool_name: str, index: int):
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": tool_name,
                        "args": {"query": f"item-{index}"},
                        "id": f"call-{index}",
                    }
                ],
            )
        ]
    }


def test_tool_frequency_limit_is_scoped_to_run_id():
    middleware = LoopDetectionMiddleware(
        warn_threshold=100,
        hard_limit=100,
        tool_freq_warn=100,
        tool_freq_hard_limit=3,
    )

    for index in range(10):
        result = middleware.after_model(
            _state("lookup", index),
            _runtime(thread_id="thread-a", loop_detection_scope_id=f"thread-a:run-{index}"),
        )
        assert result is None


def test_tool_frequency_limit_still_applies_within_same_run():
    middleware = LoopDetectionMiddleware(
        warn_threshold=100,
        hard_limit=100,
        tool_freq_warn=100,
        tool_freq_hard_limit=3,
    )

    assert middleware.after_model(_state("lookup", 1), _runtime(thread_id="thread-a", loop_detection_scope_id="thread-a:run-1")) is None
    assert middleware.after_model(_state("lookup", 2), _runtime(thread_id="thread-a", loop_detection_scope_id="thread-a:run-1")) is None

    result = middleware.after_model(
        _state("lookup", 3),
        _runtime(thread_id="thread-a", loop_detection_scope_id="thread-a:run-1"),
    )

    assert result is not None
    assert "messages" in result
