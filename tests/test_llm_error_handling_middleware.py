import pytest
from langchain_core.messages import AIMessage

from deerflow.agents.middlewares.llm_error_handling_middleware import LLMErrorHandlingMiddleware


class FakeProviderError(Exception):
    status_code = 400
    body = {
        "error": {
            "message": "Input data may contain inappropriate content.",
            "type": "data_inspection_failed",
            "code": "data_inspection_failed",
        }
    }

    def __str__(self) -> str:
        return "Error code: 400 - {'error': {'code': 'data_inspection_failed'}}"


class FakeUpstreamProviderError(Exception):
    status_code = 400
    body = {
        "error": {
            "message": "Error from provider (Console Go): Upstream request failed",
            "type": "invalid_request_error",
            "code": "invalid_request_error",
        }
    }

    def __str__(self) -> str:
        return (
            "Error code: 400 - {'error': {'message': "
            "'Error from provider (Console Go): Upstream request failed', "
            "'type': 'invalid_request_error', 'code': 'invalid_request_error'}}"
        )


def test_content_policy_error_returns_user_message_without_raising():
    middleware = LLMErrorHandlingMiddleware()

    def handler(_request):
        raise FakeProviderError()

    response = middleware.wrap_model_call(None, handler)

    assert isinstance(response, AIMessage)
    assert "content safety policy" in response.content


@pytest.mark.asyncio
async def test_async_content_policy_error_returns_user_message_without_raising():
    middleware = LLMErrorHandlingMiddleware()

    async def handler(_request):
        raise FakeProviderError()

    response = await middleware.awrap_model_call(None, handler)

    assert isinstance(response, AIMessage)
    assert "content safety policy" in response.content


def test_wrapped_upstream_failure_retries_despite_400_status():
    middleware = LLMErrorHandlingMiddleware()
    middleware.retry_base_delay_ms = 0
    attempts = 0

    def handler(_request):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise FakeUpstreamProviderError()
        return AIMessage(content="recovered")

    response = middleware.wrap_model_call(None, handler)

    assert attempts == 3
    assert isinstance(response, AIMessage)
    assert response.content == "recovered"


@pytest.mark.asyncio
async def test_async_wrapped_upstream_failure_retries_despite_400_status():
    middleware = LLMErrorHandlingMiddleware()
    middleware.retry_base_delay_ms = 0
    attempts = 0

    async def handler(_request):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise FakeUpstreamProviderError()
        return AIMessage(content="recovered")

    response = await middleware.awrap_model_call(None, handler)

    assert attempts == 3
    assert isinstance(response, AIMessage)
    assert response.content == "recovered"


def test_other_bad_requests_are_not_retried():
    middleware = LLMErrorHandlingMiddleware()
    attempts = 0

    class InvalidRequestError(Exception):
        status_code = 400

    def handler(_request):
        nonlocal attempts
        attempts += 1
        raise InvalidRequestError("Invalid tool schema")

    response = middleware.wrap_model_call(None, handler)

    assert attempts == 1
    assert isinstance(response, AIMessage)
    assert "Invalid tool schema" in response.content


class _NoHeaderError(Exception):
    """A retriable-shaped exception with no Retry-After response headers."""


def test_retry_delay_uses_full_jitter_range(monkeypatch):
    middleware = LLMErrorHandlingMiddleware()
    middleware.retry_base_delay_ms = 1000
    middleware.retry_cap_delay_ms = 8000
    calls = []

    def fake_uniform(low, high):
        calls.append((low, high))
        return high / 2

    monkeypatch.setattr(
        "deerflow.agents.middlewares.llm_error_handling_middleware.random.uniform",
        fake_uniform,
    )

    delay = middleware._build_retry_delay_ms(3, _NoHeaderError())

    # attempt=3 -> uncapped backoff = base * 2**(3-1) = 4000, capped at min(4000, 8000) = 4000.
    # Full jitter samples uniformly from [0, capped_backoff], so the lower bound must be 0
    # (never base/2 or similar) and the upper bound must equal the capped backoff exactly.
    assert calls == [(0, 4000)]
    assert delay == 2000


def test_retry_delay_never_exceeds_cap_even_at_worst_case_sample(monkeypatch):
    middleware = LLMErrorHandlingMiddleware()
    middleware.retry_base_delay_ms = 1000
    middleware.retry_cap_delay_ms = 8000

    # Worst case for the "never exceed cap" guarantee: random.uniform returns its
    # upper bound every time.
    monkeypatch.setattr(
        "deerflow.agents.middlewares.llm_error_handling_middleware.random.uniform",
        lambda low, high: high,
    )

    for attempt in range(1, 8):
        delay = middleware._build_retry_delay_ms(attempt, _NoHeaderError())
        assert 0 <= delay <= middleware.retry_cap_delay_ms

    # Once uncapped exponential growth exceeds retry_cap_delay_ms, the sampled
    # upper bound must saturate at the cap rather than keep growing.
    assert middleware._build_retry_delay_ms(6, _NoHeaderError()) == middleware.retry_cap_delay_ms


def test_retry_delay_disperses_across_repeated_calls_for_same_attempt(monkeypatch):
    """Same attempt number must not always produce the same delay (no thundering herd)."""
    middleware = LLMErrorHandlingMiddleware()
    middleware.retry_base_delay_ms = 1000
    middleware.retry_cap_delay_ms = 8000

    sampled_values = iter([100.0, 900.0, 1999.0])
    monkeypatch.setattr(
        "deerflow.agents.middlewares.llm_error_handling_middleware.random.uniform",
        lambda low, high: next(sampled_values),
    )

    delays = [middleware._build_retry_delay_ms(2, _NoHeaderError()) for _ in range(3)]

    assert delays == [100, 900, 1999]
    assert len(set(delays)) == len(delays)


def test_terminal_upstream_failure_emits_machine_readable_event(monkeypatch):
    events = []
    middleware = LLMErrorHandlingMiddleware()
    middleware.retry_max_attempts = 1
    monkeypatch.setattr("langgraph.config.get_stream_writer", lambda: events.append)

    def handler(_request):
        raise FakeUpstreamProviderError()

    response = middleware.wrap_model_call(None, handler)

    assert isinstance(response, AIMessage)
    assert events == [
        {
            "type": "llm_failure",
            "reason": "busy",
            "retriable": True,
            "message": "The configured LLM provider is temporarily unavailable after multiple retries. Please wait a moment and continue the conversation.",
        }
    ]
