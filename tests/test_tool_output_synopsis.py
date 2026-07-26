import json

from deerflow.agents.middlewares.tool_output_synopsis import build_synopsis


def test_json_object_synopsis_describes_keys_and_types() -> None:
    payload = {
        "status": "ok",
        "count": 3,
        "ratio": 0.5,
        "items": [{"id": 1, "name": "alpha"}, {"id": 2, "name": "beta"}, {"id": 3, "name": "gamma"}],
        "meta": {"page": 1, "nested": {"deep": {"value": "too deep to expand"}}},
    }
    content = json.dumps(payload)

    result = build_synopsis(content, tool_name="web_search", max_chars=4_000)

    assert result is not None
    assert "[Structured synopsis of web_search output: JSON]" in result
    assert "object (5 keys)" in result
    assert "status: str = " in result
    assert "count: int = 3" in result
    assert "ratio: float = 0.5" in result
    assert "items: array (len=3)" in result
    assert "[0]: object (2 keys)" in result
    assert "element types: object×3 (sampled 3 of 3)" in result
    assert "meta: object (2 keys)" in result


def test_json_array_synopsis_reports_length_and_first_element_shape() -> None:
    payload = [{"id": i, "ok": True} for i in range(10)]
    content = json.dumps(payload)

    result = build_synopsis(content, tool_name="bash", max_chars=4_000)

    assert result is not None
    assert "array (len=10)" in result
    assert "[0]: object (2 keys)" in result
    assert "id: int = 0" in result
    assert "element types: object×10 (sampled 10 of 10)" in result


def test_jsonl_synopsis_reports_line_count_and_common_keys() -> None:
    rows = [{"id": i, "name": f"row-{i}", "extra": i} if i != 4 else {"id": i, "name": f"row-{i}"} for i in range(8)]
    content = "\n".join(json.dumps(row) for row in rows)

    result = build_synopsis(content, tool_name="db_query", max_chars=4_000)

    assert result is not None
    assert "JSON Lines, 8 lines total (8 sampled)" in result
    assert "first line shape:" in result
    assert "common keys across sampled lines: id, name" in result


def test_non_json_content_returns_none() -> None:
    assert build_synopsis("just some plain log output\nnothing structured here", tool_name="bash", max_chars=4_000) is None


def test_malformed_json_returns_none() -> None:
    assert build_synopsis('{"a": 1, "b": [1, 2,}', tool_name="bash", max_chars=4_000) is None


def test_single_jsonl_like_line_is_not_treated_as_jsonl() -> None:
    # A single valid JSON line is just a JSON value - already handled by the
    # full-document parse path - so it should not fall into JSONL detection
    # requiring 2+ lines. Since it parses fine as whole-document JSON, it
    # still produces a synopsis (as JSON, not JSONL).
    result = build_synopsis('{"only": "one line"}', tool_name="bash", max_chars=4_000)
    assert result is not None
    assert "[Structured synopsis of bash output: JSON]" in result


def test_mixed_valid_and_invalid_lines_is_not_jsonl() -> None:
    content = '{"a": 1}\nnot json at all\n{"a": 2}'
    assert build_synopsis(content, tool_name="bash", max_chars=4_000) is None


def test_large_array_is_sampled_not_fully_scanned() -> None:
    payload = [{"idx": i} for i in range(50_000)]
    content = json.dumps(payload)

    result = build_synopsis(content, tool_name="bash", max_chars=4_000)

    assert result is not None
    assert "array (len=50000)" in result
    assert "sampled 50 of 50000" in result


def test_max_chars_is_a_hard_limit() -> None:
    payload = {f"key_{i}": {"nested": list(range(i))} for i in range(200)}
    content = json.dumps(payload)

    result = build_synopsis(content, tool_name="bash", max_chars=200)

    assert result is not None
    assert len(result) <= 200


def test_max_chars_zero_returns_none() -> None:
    assert build_synopsis('{"a": 1}', tool_name="bash", max_chars=0) is None


def test_deep_nesting_collapses_after_max_depth() -> None:
    payload = {"a": {"b": {"c": {"d": {"e": "too deep"}}}}}
    content = json.dumps(payload)

    result = build_synopsis(content, tool_name="bash", max_chars=4_000)

    assert result is not None
    # root (depth 0) and "a" (depth 1) expand their children individually;
    # "b" is reached at depth 2 and collapses to a keys-only preview, so its
    # child "c" is named but not itself expanded into its own object line.
    assert "a: object (1 keys)" in result
    assert "b: object (1 keys)" in result
    assert "keys: c" in result
    assert "c: object" not in result
    # Everything nested under the collapsed level is not walked further.
    assert '"e"' not in result
