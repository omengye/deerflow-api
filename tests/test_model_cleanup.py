from typing import Any

import pytest

from deerflow.models import aclose_chat_model


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeModel:
    def __init__(self, **attrs: Any) -> None:
        for name, value in attrs.items():
            setattr(self, name, value)


@pytest.mark.asyncio
async def test_aclose_chat_model_only_closes_injected_http_client() -> None:
    root_client = _FakeClient()
    http_client = _FakeClient()
    model = _FakeModel(root_async_client=root_client, http_async_client=http_client)

    await aclose_chat_model(model)

    assert http_client.closed is True
    assert root_client.closed is False


@pytest.mark.asyncio
async def test_aclose_chat_model_closes_root_client_without_injected_http_client() -> None:
    root_client = _FakeClient()
    model = _FakeModel(root_async_client=root_client)

    await aclose_chat_model(model)

    assert root_client.closed is True
