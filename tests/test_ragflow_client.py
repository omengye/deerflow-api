from __future__ import annotations

import httpx
import pytest

from deerflow.community.ragflow.client import (
    RAGFlowAPIError,
    RAGFlowClient,
    RAGFlowProtocolError,
)


async def test_client_uses_bearer_auth_and_ids_filter() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"code": 0, "data": [{"id": "dataset-1"}]},
        )

    client = RAGFlowClient(
        base_url="http://ragflow.test/",
        api_key="secret",
        transport=httpx.MockTransport(handler),
    )

    result = await client.list_datasets(dataset_id="dataset-1")

    assert result == [{"id": "dataset-1"}]
    assert requests[0].url.path == "/api/v1/datasets"
    assert requests[0].url.params["ids"] == "dataset-1"
    assert requests[0].headers["authorization"] == "Bearer secret"


async def test_client_paginates_dataset_catalog() -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        data = (
            [{"id": f"dataset-{index}"} for index in range(100)]
            if page == 1
            else [{"id": "last"}]
        )
        return httpx.Response(200, json={"code": 0, "data": data, "total": 101})

    client = RAGFlowClient(
        base_url="http://ragflow.test",
        api_key="secret",
        transport=httpx.MockTransport(handler),
    )

    result = await client.list_datasets()

    assert pages == [1, 2]
    assert len(result) == 101


async def test_client_posts_explicit_retrieval_scope() -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(__import__("json").loads(request.content))
        return httpx.Response(
            200,
            json={"code": 0, "data": {"chunks": [], "doc_aggs": []}},
        )

    client = RAGFlowClient(
        base_url="http://ragflow.test",
        api_key="secret",
        transport=httpx.MockTransport(handler),
    )

    await client.retrieve(
        "question",
        dataset_ids=["dataset-1"],
        page_size=8,
        similarity_threshold=0.2,
        vector_similarity_weight=0.3,
        top_k=256,
    )

    assert captured[0]["dataset_ids"] == ["dataset-1"]
    assert captured[0]["question"] == "question"


async def test_client_normalizes_provider_and_protocol_errors() -> None:
    api_client = RAGFlowClient(
        base_url="http://ragflow.test",
        api_key="secret",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                400,
                json={"code": 102, "message": "bad secret"},
            )
        ),
    )
    with pytest.raises(RAGFlowAPIError, match=r"bad \[REDACTED\]"):
        await api_client.list_datasets()

    protocol_client = RAGFlowClient(
        base_url="http://ragflow.test",
        api_key="secret",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, text="not-json")
        ),
    )
    with pytest.raises(RAGFlowProtocolError, match="invalid JSON"):
        await protocol_client.list_datasets()
