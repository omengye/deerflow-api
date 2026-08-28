"""Compact, citation-friendly rendering of RAGFlow chunks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _truncate(text: str, limit: int, marker: str = "…") -> str:
    if len(text) <= limit:
        return text
    if limit <= len(marker):
        return marker[:limit]
    return f"{text[: limit - len(marker)].rstrip()}{marker}"


def _score(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _aggregates(value: object) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        return [item for item in value.values() if isinstance(item, Mapping)]
    return []


def format_retrieval_result(
    result: Mapping[str, Any],
    *,
    dataset_names_by_id: Mapping[str, str],
    max_chars_per_chunk: int,
    max_total_chars: int,
) -> str:
    """Render provider-normalized chunks without exposing dataset IDs."""
    raw_chunks = result.get("chunks")
    chunks = (
        [item for item in raw_chunks if isinstance(item, Mapping)]
        if isinstance(raw_chunks, list)
        else []
    )
    if not chunks:
        return "No relevant content found."

    aggregates = _aggregates(result.get("doc_aggs"))
    document_names = {
        str(item["doc_id"]): str(item["doc_name"])
        for item in aggregates
        if item.get("doc_id") and item.get("doc_name")
    }

    entries: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        dataset_name = dataset_names_by_id.get(
            str(chunk.get("dataset_id")),
            "Unknown dataset",
        )
        document_id = chunk.get("document_id")
        document_name = chunk.get("document_keyword")
        if not document_name and document_id is not None:
            document_name = document_names.get(str(document_id))
        document_name = str(document_name or "Unknown document")
        similarity = _score(chunk.get("similarity"))
        score_text = f"  (score {similarity:.2f})" if similarity is not None else ""
        content = _truncate(
            str(chunk.get("content") or "").strip(),
            max_chars_per_chunk,
        )
        entries.append(
            f"[{index}] {dataset_name} / {document_name}{score_text}\n{content}"
        )

    summaries: list[str] = []
    for item in aggregates:
        name = item.get("doc_name")
        if not name:
            continue
        count = item.get("count")
        count_text = (
            str(count)
            if isinstance(count, int) and not isinstance(count, bool)
            else "?"
        )
        unit = "chunk" if count == 1 else "chunks"
        summaries.append(f"{name} ({count_text} {unit})")
    if summaries:
        entries.append(f"Matched documents: {', '.join(summaries)}")

    rendered = "\n\n".join(entries)
    marker = "… (response truncated)"
    if len(rendered) <= max_total_chars:
        return rendered
    if max_total_chars <= len(marker):
        return marker[:max_total_chars]
    return f"{rendered[: max_total_chars - len(marker)].rstrip()}{marker}"
