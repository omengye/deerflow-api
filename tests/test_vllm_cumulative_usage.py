from langchain_core.messages import AIMessageChunk

from langchain_core.messages.ai import UsageMetadata

from deerflow.models.vllm_provider import (
    VllmChatModel,
    _CUMULATIVE_USAGE_TRACKER_CAPACITY,
)


def _model(*, cumulative: bool) -> VllmChatModel:
    return VllmChatModel(
        model="Qwen/QwQ-32B",
        api_key="dummy",
        base_url="http://localhost:8000/v1",
        cumulative_stream_usage=cumulative,
    )


def _chunk(completion_id: str, prompt: int, completion: int, *, choices: bool = True) -> dict:
    return {
        "id": completion_id,
        "model": "Qwen/QwQ-32B",
        "choices": ([{"delta": {"role": "assistant", "content": "x"}, "finish_reason": None}] if choices else []),
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


def _convert(model: VllmChatModel, chunk: dict):
    return model._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, {})


def test_vllm_cumulative_stream_usage_is_converted_to_deltas_and_cleared() -> None:
    model = _model(cumulative=True)

    first = _convert(model, _chunk("completion-1", 10, 1))
    second = _convert(model, _chunk("completion-1", 10, 4))
    terminal = _convert(model, _chunk("completion-1", 10, 4, choices=False))

    assert {key: first.message.usage_metadata[key] for key in ("input_tokens", "output_tokens", "total_tokens")} == {
        "input_tokens": 10,
        "output_tokens": 1,
        "total_tokens": 11,
    }
    assert {key: second.message.usage_metadata[key] for key in ("input_tokens", "output_tokens", "total_tokens")} == {
        "input_tokens": 0,
        "output_tokens": 3,
        "total_tokens": 3,
    }
    assert {key: terminal.message.usage_metadata[key] for key in ("input_tokens", "output_tokens", "total_tokens")} == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    assert not model._cumulative_usage_by_completion


def test_vllm_cumulative_usage_mode_is_opt_in() -> None:
    model = _model(cumulative=False)
    _convert(model, _chunk("completion-1", 10, 1))
    second = _convert(model, _chunk("completion-1", 10, 4))

    assert second.message.usage_metadata["input_tokens"] == 10
    assert second.message.usage_metadata["output_tokens"] == 4
    assert second.message.usage_metadata["total_tokens"] == 14


def test_vllm_choice_terminal_frame_emits_final_delta_and_clears_snapshot() -> None:
    model = _model(cumulative=True)
    _convert(model, _chunk("completion-1", 10, 1))
    terminal_chunk = _chunk("completion-1", 10, 4)
    terminal_chunk["choices"][0]["finish_reason"] = "stop"

    terminal = _convert(model, terminal_chunk)

    assert terminal.message.usage_metadata["output_tokens"] == 3
    assert not model._cumulative_usage_by_completion


def test_vllm_usage_tracker_has_a_hard_capacity() -> None:
    model = _model(cumulative=True)
    usage = UsageMetadata(input_tokens=1, output_tokens=1, total_tokens=2)

    for index in range(_CUMULATIVE_USAGE_TRACKER_CAPACITY + 10):
        model._usage_delta(f"completion-{index}", usage, terminal=False)

    assert len(model._cumulative_usage_by_completion) == _CUMULATIVE_USAGE_TRACKER_CAPACITY
    assert "completion-0" not in model._cumulative_usage_by_completion
