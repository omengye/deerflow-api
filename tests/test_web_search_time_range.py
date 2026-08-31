import sys
from types import SimpleNamespace

from deerflow.community.brave_search import tools as brave_tools
from deerflow.community.ddg_search import tools as ddg_tools
from deerflow.community.search_time_range import (
    BRAVE_FRESHNESS_BY_TIME_RANGE,
    DDGS_TIMELIMIT_BY_TIME_RANGE,
)


def test_provider_time_range_mappings_are_complete() -> None:
    expected = {"day", "week", "month", "year"}

    assert set(DDGS_TIMELIMIT_BY_TIME_RANGE) == expected
    assert set(BRAVE_FRESHNESS_BY_TIME_RANGE) == expected


def test_ddg_recency_uses_native_timelimit_and_supported_backends(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeDDGS:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def text(self, query, **kwargs):
            captured["query"] = query
            captured["search"] = kwargs
            return [{"title": "result"}]

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=FakeDDGS))

    results = ddg_tools._search_text(
        "recent changes",
        time_range="week",
        https_proxy="http://proxy.invalid",
        timeout=9,
    )

    assert results == [{"title": "result"}]
    assert captured["client"] == {
        "proxy": "http://proxy.invalid",
        "timeout": 9,
    }
    assert captured["search"]["timelimit"] == "w"
    assert captured["search"]["backend"] == "brave,duckduckgo,yahoo"


def test_ddg_without_recency_preserves_default_backend_selection(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeDDGS:
        def __init__(self, **kwargs):
            pass

        def text(self, query, **kwargs):
            captured.update(kwargs)
            return []

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=FakeDDGS))

    ddg_tools._search_text("general query")

    assert "timelimit" not in captured
    assert "backend" not in captured


def test_brave_recency_maps_to_freshness_parameter(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"web": {"results": [{"title": "result"}]}}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, *, headers, params):
            captured["url"] = url
            captured["headers"] = headers
            captured["params"] = params
            return FakeResponse()

    monkeypatch.setattr(brave_tools.httpx, "Client", FakeClient)

    results = brave_tools._search_text(
        "recent changes",
        api_key="secret",
        time_range="month",
    )

    assert results == [{"title": "result"}]
    assert captured["params"]["freshness"] == "pm"


def test_search_tool_schemas_expose_time_range_enum() -> None:
    for search_tool in (ddg_tools.web_search_tool, brave_tools.web_search_tool):
        schema = search_tool.get_input_schema().model_json_schema()
        time_range_schema = schema["properties"]["time_range"]
        time_range_ref = time_range_schema["anyOf"][0]["$ref"].rsplit("/", 1)[-1]

        assert set(schema["$defs"][time_range_ref]["enum"]) == {
            "day",
            "week",
            "month",
            "year",
        }
