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
