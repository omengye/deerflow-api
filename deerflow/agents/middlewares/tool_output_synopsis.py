"""Deterministic structural previews ("synopses") for oversized tool outputs.

When ``tool_output_budget_middleware`` externalizes an oversized tool result to
disk, the model only sees a head/tail character slice of the raw text (see
``_build_preview``). For structured payloads - JSON objects/arrays, JSON
Lines logs - that slice is frequently a half-parsed fragment (an open brace,
a dangling comma) that tells the model nothing about the overall shape of the
data. ``build_synopsis`` produces a compact, deterministic textual schema
summary instead: top-level keys and value types, array lengths, a
depth-limited sample of nested structure, and representative scalar values.

This module never raises: any parsing or formatting failure is swallowed and
``None`` is returned, so callers can unconditionally fall back to the
existing head/tail preview.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Containers are expanded while their nesting depth is below this value;
# at or beyond it they collapse to a header line plus a short keys/types
# preview instead of recursing further. Depth 0 is the top-level value, so
# this yields two fully-expanded levels with a peek at a third.
_MAX_EXPAND_DEPTH = 2
# Cap on how many elements/keys are inspected when describing a container.
# Arrays and objects are sampled rather than walked in full so a pathological
# multi-million-element payload can't make synopsis generation itself slow.
_SAMPLE_SIZE = 50
# Longest a single scalar preview value is allowed to be before truncation.
_SCALAR_PREVIEW_LEN = 80
# How many leading non-blank lines of a candidate JSON Lines payload are
# parsed to decide whether the content actually is JSON Lines.
_JSONL_SNIFF_LINES = 20
# Minimum non-blank line count before content is even considered for JSONL
# detection (a single line is just a JSON value, already handled above).
_MIN_JSONL_LINES = 2


def build_synopsis(content: str, *, tool_name: str, max_chars: int) -> str | None:
    """Return a deterministic structural summary of ``content``, or ``None``.

    ``None`` means the caller should fall back to the existing head/tail
    preview - either because ``content`` isn't recognizably JSON or JSON
    Lines, or because something went wrong while summarizing it.
    """
    try:
        return _build_synopsis(content, tool_name=tool_name, max_chars=max_chars)
    except Exception:
        # Most tool outputs aren't JSON/JSONL at all, so hitting this branch
        # is routine, not exceptional; log at debug (with the traceback) so a
        # real bug in the describer logic is still diagnosable without
        # flooding logs for the common "not structured" case.
        logger.debug("Failed to build structured synopsis for %s output", tool_name, exc_info=True)
        return None


def _build_synopsis(content: str, *, tool_name: str, max_chars: int) -> str | None:
    if max_chars <= 0:
        return None
    stripped = content.strip()
    if not stripped:
        return None

    try:
        value = json.loads(stripped)
    except ValueError:
        pass
    else:
        return _render(_describe_json(value, tool_name), max_chars)

    sniffed = _sniff_jsonl(stripped)
    if sniffed is None:
        return None
    total_lines, rows = sniffed
    return _render(_describe_jsonl(total_lines, rows, tool_name), max_chars)


def _sniff_jsonl(stripped: str) -> tuple[int, list[Any]] | None:
    lines = [line for line in stripped.split("\n") if line.strip()]
    if len(lines) < _MIN_JSONL_LINES:
        return None

    rows: list[Any] = []
    for line in lines[:_JSONL_SNIFF_LINES]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            return None
    return len(lines), rows


def _describe_json(value: Any, tool_name: str) -> list[str]:
    header = f"[Structured synopsis of {tool_name} output: JSON]"
    return [header, *_describe_node(value, depth=0, indent="")]


def _describe_jsonl(total_lines: int, rows: list[Any], tool_name: str) -> list[str]:
    lines = [
        f"[Structured synopsis of {tool_name} output: JSON Lines, {total_lines} lines total ({len(rows)} sampled)]",
        "first line shape:",
    ]
    lines.extend(_describe_node(rows[0], depth=0, indent="  "))

    dict_rows = [row for row in rows if isinstance(row, dict)]
    if dict_rows:
        common_keys = set(dict_rows[0].keys())
        for row in dict_rows[1:]:
            common_keys &= set(row.keys())
        ordered = [key for key in dict_rows[0] if key in common_keys]
        summary = ", ".join(ordered) if ordered else "(none)"
        lines.append(f"common keys across sampled lines: {summary}")
    else:
        counts = _sample_type_counts(rows)
        lines.append(f"sampled line types: {_format_type_counts(counts)}")
    return lines


def _describe_node(value: Any, *, depth: int, indent: str, label: str = "") -> list[str]:
    prefix = f"{indent}{label}"

    if isinstance(value, dict):
        header = f"{prefix}object ({len(value)} keys)"
        if not value:
            return [header]
        if depth >= _MAX_EXPAND_DEPTH:
            keys_preview = ", ".join(list(value.keys())[:_SAMPLE_SIZE])
            return [header, f"{indent}  keys: {keys_preview}"]

        lines = [header]
        child_indent = indent + "  "
        items = list(value.items())
        sample = items[:_SAMPLE_SIZE]
        for key, val in sample:
            lines.extend(_describe_node(val, depth=depth + 1, indent=child_indent, label=f"{key}: "))
        omitted = len(items) - len(sample)
        if omitted > 0:
            lines.append(f"{child_indent}... ({omitted} more keys omitted)")
        return lines

    if isinstance(value, list):
        header = f"{prefix}array (len={len(value)})"
        if not value:
            return [header]

        lines = [header]
        child_indent = indent + "  "
        if depth < _MAX_EXPAND_DEPTH:
            lines.extend(_describe_node(value[0], depth=depth + 1, indent=child_indent, label="[0]: "))
        counts = _sample_type_counts(value)
        sampled_n = min(len(value), _SAMPLE_SIZE)
        lines.append(f"{child_indent}element types: {_format_type_counts(counts)} (sampled {sampled_n} of {len(value)})")
        return lines

    return [f"{prefix}{_type_name(value)} = {_scalar_preview(value)}"]


def _sample_type_counts(items: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items[:_SAMPLE_SIZE]:
        name = _type_name(item)
        counts[name] = counts.get(name, 0) + 1
    return counts


def _format_type_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{name}×{count}" for name, count in counts.items())


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _scalar_preview(value: Any) -> str:
    if isinstance(value, str):
        preview = value if len(value) <= _SCALAR_PREVIEW_LEN else value[:_SCALAR_PREVIEW_LEN] + "…"
        return json.dumps(preview, ensure_ascii=False)
    if value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value)
    return _type_name(value)


def _render(lines: list[str], max_chars: int) -> str:
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    marker = "\n... (synopsis truncated)"
    if len(marker) >= max_chars:
        return text[:max_chars]
    return text[: max_chars - len(marker)] + marker
