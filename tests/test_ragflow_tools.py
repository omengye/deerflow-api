from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from deerflow.community.ragflow import tools as ragflow_tools
from deerflow.community.ragflow.client import RAGFlowAPIError
from deerflow.community.ragflow.formatting import format_retrieval_result
from deerflow.config.tool_config import ToolConfig

DATASET_A = "0123456789abcdef0123456789abcdef"
DATASET_B = "fedcba9876543210fedcba9876543210"


def _dataset(
    dataset_id: str,
    name: str,
    *,
    model: str = "embedding-v3",
    chunks: int = 1,
) -> dict:
    return {
        "id": dataset_id,
        "name": name,
        "embedding_model": model,
        "chunk_count": chunks,
    }


class _FakeClient:
    def __init__(
        self,
        *,
        by_id: Mapping[str, list[dict]] | None = None,
        all_datasets: list[dict] | None = None,
        results: Mapping[tuple[str, ...], dict] | None = None,
        errors: Mapping[tuple[str, ...], Exception] | None = None,
    ) -> None:
        self.by_id = dict(by_id or {})
        self.all_datasets = list(all_datasets or [])
        self.results = dict(results or {})
        self.errors = dict(errors or {})
        self.list_calls: list[str | None] = []
        self.retrieve_calls: list[tuple[str, dict]] = []

    async def list_datasets(self, *, dataset_id=None):
        self.list_calls.append(dataset_id)
        return (
            self.all_datasets if dataset_id is None else self.by_id.get(dataset_id, [])
        )

    async def retrieve(self, query: str, **kwargs):
        self.retrieve_calls.append((query, kwargs))
        key = tuple(kwargs["dataset_ids"])
        if key in self.errors:
            raise self.errors[key]
        return self.results.get(key, {"chunks": [], "doc_aggs": []})


def _config(
    *,
    api_key: str | None = "ragflow-secret",
    datasets: list[str] | None = None,
    page_size: int = 8,
    configured: bool = True,
):
    extra = {
        "base_url": "http://ragflow.test",
        "api_key": api_key,
        "page_size": page_size,
    }
    if datasets is not None:
        extra["datasets"] = datasets
    tool = ToolConfig(
        name="knowledge_search",
        group="knowledge",
        use="deerflow.community.ragflow.tools:knowledge_search_tool",
        **extra,
    )
    return SimpleNamespace(
        get_tool_config=lambda name: (
            tool if configured and name == "knowledge_search" else None
        )
    )


def _install(monkeypatch, client: _FakeClient, *, config=None) -> None:
    monkeypatch.setattr(
        ragflow_tools,
        "get_app_config",
        lambda: config or _config(datasets=[DATASET_A]),
    )
    monkeypatch.setattr(ragflow_tools, "_build_client", lambda _settings: client)


@pytest.fixture(autouse=True)
def _clear_warnings() -> None:
    ragflow_tools._warned.clear()


async def test_search_resolves_operator_dataset_ids_and_hides_them(monkeypatch) -> None:
    client = _FakeClient(
        by_id={
            DATASET_A: [_dataset(DATASET_A, "HR Policies")],
            DATASET_B: [_dataset(DATASET_B, "Engineering")],
        },
        results={
            (DATASET_A, DATASET_B): {
                "chunks": [
                    {
                        "dataset_id": DATASET_A,
                        "document_id": "doc-1",
                        "document_keyword": "handbook.pdf",
                        "content": "Annual leave is based on service years.",
                        "similarity": 0.874,
                    }
                ],
                "doc_aggs": [
                    {"doc_id": "doc-1", "doc_name": "handbook.pdf", "count": 1}
                ],
            }
        },
    )
    _install(
        monkeypatch,
        client,
        config=_config(datasets=[DATASET_A, DATASET_B]),
    )

    result = await ragflow_tools.knowledge_search("annual leave")

    assert client.list_calls == [DATASET_A, DATASET_B]
    assert client.retrieve_calls[0][1]["dataset_ids"] == [DATASET_A, DATASET_B]
    assert "[1] HR Policies / handbook.pdf  (score 0.87)" in result
    assert "Matched documents: handbook.pdf (1 chunk)" in result
    assert DATASET_A not in result
    assert DATASET_B not in result


async def test_missing_bound_dataset_fails_closed_without_leaking_id(
    monkeypatch,
) -> None:
    client = _FakeClient()
    _install(monkeypatch, client, config=_config(datasets=[DATASET_A]))

    result = await ragflow_tools.knowledge_search("leave")

    assert result == (
        "Error: knowledge_search.datasets entry 1 was not found or is "
        "inaccessible; check config.yaml."
    )
    assert DATASET_A not in result
    assert client.retrieve_calls == []


async def test_all_dataset_mode_groups_incompatible_embeddings(monkeypatch) -> None:
    client = _FakeClient(
        all_datasets=[
            _dataset(DATASET_A, "Legacy", model="embedding-v2"),
            _dataset(DATASET_B, "Current", model="embedding-v3"),
        ],
        results={
            (DATASET_A,): {
                "chunks": [
                    {
                        "dataset_id": DATASET_A,
                        "document_keyword": "legacy.txt",
                        "content": "legacy result",
                        "similarity": 0.99,
                    }
                ]
            },
            (DATASET_B,): {
                "chunks": [
                    {
                        "dataset_id": DATASET_B,
                        "document_keyword": "current.txt",
                        "content": "current result",
                        "similarity": 0.50,
                    }
                ]
            },
        },
    )
    _install(monkeypatch, client, config=_config(datasets=None, page_size=2))

    result = await ragflow_tools.knowledge_search("policy")

    assert {tuple(call[1]["dataset_ids"]) for call in client.retrieve_calls} == {
        (DATASET_A,),
        (DATASET_B,),
    }
    assert "legacy result" in result and "current result" in result
    assert "score" not in result


async def test_empty_datasets_are_not_retrieved(monkeypatch) -> None:
    client = _FakeClient(
        all_datasets=[
            _dataset(DATASET_A, "Empty", model="", chunks=0),
            _dataset(DATASET_B, "Current", chunks=2),
        ],
        results={
            (DATASET_B,): {
                "chunks": [
                    {
                        "dataset_id": DATASET_B,
                        "document_keyword": "guide.md",
                        "content": "searchable",
                    }
                ]
            }
        },
    )
    _install(monkeypatch, client, config=_config(datasets=None))

    result = await ragflow_tools.knowledge_search("guide")

    assert [call[1]["dataset_ids"] for call in client.retrieve_calls] == [[DATASET_B]]
    assert "searchable" in result


async def test_retrieval_groups_have_bounded_concurrency(monkeypatch) -> None:
    class _TrackingClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__(
                all_datasets=[
                    _dataset(
                        f"dataset-{index}", f"Dataset {index}", model=f"model-{index}"
                    )
                    for index in range(6)
                ]
            )
            self.active = 0
            self.maximum = 0

        async def retrieve(self, query: str, **kwargs):
            self.retrieve_calls.append((query, kwargs))
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            try:
                await asyncio.sleep(0.03)
                return {"chunks": [], "doc_aggs": []}
            finally:
                self.active -= 1

    client = _TrackingClient()
    _install(monkeypatch, client, config=_config(datasets=None))

    assert await ragflow_tools.knowledge_search("anything") == (
        "No relevant content found."
    )
    assert client.maximum == 4


async def test_api_key_is_redacted_from_success_and_error(monkeypatch) -> None:
    successful = _FakeClient(
        by_id={DATASET_A: [_dataset(DATASET_A, "Policies")]},
        results={
            (DATASET_A,): {
                "chunks": [
                    {
                        "dataset_id": DATASET_A,
                        "document_keyword": "secret.txt",
                        "content": "provider echoed ragflow-secret",
                    }
                ]
            }
        },
    )
    _install(monkeypatch, successful)
    success = await ragflow_tools.knowledge_search("secret")
    assert "ragflow-secret" not in success
    assert "[REDACTED]" in success

    failed = _FakeClient(
        by_id={DATASET_A: [_dataset(DATASET_A, "Policies")]},
        errors={
            (DATASET_A,): RAGFlowAPIError(
                f"dataset {DATASET_A} rejected ragflow-secret",
                code=102,
            )
        },
    )
    _install(monkeypatch, failed)
    error = await ragflow_tools.knowledge_search("secret")
    assert DATASET_A not in error
    assert "ragflow-secret" not in error
    assert "[DATASET_ID]" in error and "[REDACTED]" in error


async def test_missing_or_invalid_configuration_returns_guidance(monkeypatch) -> None:
    client = _FakeClient()
    _install(monkeypatch, client, config=_config(configured=False))
    assert "not configured" in await ragflow_tools.knowledge_search("query")

    _install(monkeypatch, client, config=_config(api_key=None))
    assert "API key is not configured" in await ragflow_tools.knowledge_search("query")

    _install(monkeypatch, client, config=_config(datasets=[]))
    assert await ragflow_tools.knowledge_search("query") == (
        "Error: Invalid RAGFlow settings; check config.yaml."
    )
    assert client.list_calls == []


def test_tool_exposes_only_query_and_no_operator_secrets() -> None:
    schema = ragflow_tools.knowledge_search_tool.tool_call_schema
    assert set(schema.model_fields) == {"query"}
    assert DATASET_A not in ragflow_tools.knowledge_search_tool.description
    assert "api_key" not in ragflow_tools.knowledge_search_tool.description


def test_formatter_bounds_chunk_and_total_output() -> None:
    result = format_retrieval_result(
        {
            "chunks": [
                {
                    "dataset_id": DATASET_A,
                    "document_keyword": f"document-{index}.txt",
                    "content": "abcdefghij" * 20,
                }
                for index in range(4)
            ]
        },
        dataset_names_by_id={DATASET_A: "Policies"},
        max_chars_per_chunk=20,
        max_total_chars=120,
    )
    assert len(result) <= 120
    assert result.endswith("… (response truncated)")
    assert DATASET_A not in result
