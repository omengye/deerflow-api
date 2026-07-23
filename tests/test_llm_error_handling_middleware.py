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
