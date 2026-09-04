"""Deterministic, message-carried receipts for subagent tool calls."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import TypedDict

from langchain_core.messages import AIMessage, ToolMessage

TOOL_RECEIPT_KEY = "deerflow_tool_receipt"
TOOL_RECEIPT_LEDGER_KEY = "deerflow_tool_receipt_ledger"
RECEIPT_ID_PREFIX = "r"

_HASH_LEN = 16
_RENDER_CHAR_BUDGET = 2000
_MAX_RECEIPT_ID_DIGITS = 10
CITATION_RE = re.compile(rf"\[{RECEIPT_ID_PREFIX}(\d+)(?:\s+([A-Za-z_][\w.-]*))?\]")


class ToolReceipt(TypedDict):
    id: str
    tool_call_id: str
    tool_name: str
    status: str
    args_sha256: str
    output_sha256: str
    output_bytes: int
    created_at: str


def receipt_id(position: int) -> str:
    return f"{RECEIPT_ID_PREFIX}{position}"


def format_citation(rid: str, tool_name: str | None = None) -> str:
    return f"[{rid} {tool_name}]" if tool_name else f"[{rid}]"


def parse_citations(text: str) -> list[tuple[str, str | None]]:
    seen: set[tuple[str, str | None]] = set()
    citations: list[tuple[str, str | None]] = []
    for match in CITATION_RE.finditer(text):
        digits = match.group(1)
        if len(digits) > _MAX_RECEIPT_ID_DIGITS:
            continue
        citation = (receipt_id(int(digits)), match.group(2))
        if citation not in seen:
            seen.add(citation)
            citations.append(citation)
    return citations


def _short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:_HASH_LEN]


def make_tool_receipt(tool_call: dict, message: ToolMessage) -> dict:
    """Build immutable execution metadata for one tool call/result pair."""
    args = tool_call.get("args")
    args_bytes = json.dumps(
        args if isinstance(args, dict) else {},
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    content = (
        message.content
        if isinstance(message.content, str)
        else json.dumps(message.content, sort_keys=True, default=str)
    )
    raw_status = getattr(message, "status", None)
    status = (
        "error" if raw_status == "error" or content.startswith("Error:") else "success"
    )
    return {
        "tool_call_id": str(tool_call.get("id") or ""),
        "tool_name": str(tool_call.get("name") or ""),
        "status": status,
        "args_sha256": _short_hash(args_bytes),
        "output_sha256": _short_hash(content.encode("utf-8")),
        "output_bytes": len(content.encode("utf-8")),
        "created_at": datetime.now(UTC).isoformat(),
    }


_RECEIPT_STR_FIELDS = (
    "tool_call_id",
    "tool_name",
    "status",
    "args_sha256",
    "output_sha256",
    "created_at",
)


def is_valid_receipt(receipt: object) -> bool:
    if not isinstance(receipt, dict):
        return False
    if any(not isinstance(receipt.get(field), str) for field in _RECEIPT_STR_FIELDS):
        return False
    output_bytes = receipt.get("output_bytes")
    return isinstance(output_bytes, int) and not isinstance(output_bytes, bool)


def extract_tool_receipts(messages: list) -> list[ToolReceipt]:
    receipts: list[ToolReceipt] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        raw = (message.additional_kwargs or {}).get(TOOL_RECEIPT_KEY)
        if not is_valid_receipt(raw):
            continue
        receipts.append(
            ToolReceipt(
                id=receipt_id(len(receipts) + 1),
                tool_call_id=raw["tool_call_id"],
                tool_name=raw["tool_name"],
                status=raw["status"],
                args_sha256=raw["args_sha256"],
                output_sha256=raw["output_sha256"],
                output_bytes=raw["output_bytes"],
                created_at=raw["created_at"],
            )
        )
    return receipts


def extract_citing_turn_receipts(messages: list) -> list[ToolReceipt] | None:
    """Return the exact ledger snapshot visible to the final citing turn."""
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        raw_ledger = (message.additional_kwargs or {}).get(TOOL_RECEIPT_LEDGER_KEY)
        if raw_ledger is None:
            continue
        if not isinstance(raw_ledger, list):
            return None
        receipts: list[ToolReceipt] = []
        first_position: int | None = None
        for index, raw in enumerate(raw_ledger):
            if not is_valid_receipt(raw):
                return None
            rid = raw.get("id")
            match = (
                re.fullmatch(rf"{RECEIPT_ID_PREFIX}([1-9]\d*)", rid)
                if isinstance(rid, str)
                else None
            )
            if match is None:
                return None
            if first_position is None:
                first_position = int(match.group(1))
            if rid != receipt_id(first_position + index):
                return None
            receipts.append(ToolReceipt(**raw))
        return receipts
    return None


def render_tool_receipts_with_snapshot(
    receipts: list[ToolReceipt],
    *,
    max_chars: int = _RENDER_CHAR_BUDGET,
) -> tuple[str, list[ToolReceipt]]:
    if not receipts:
        return "", []
    lines = [
        "## Tool receipts (execution record)",
        f"Cite receipt ids (e.g. {format_citation(receipt_id(1), 'write_file')}) in your final report for every claim about an action you took.",
        "Execution evidence only; receipts record that a call happened and its status, not that the task is correct or accepted.",
    ]
    receipt_lines = [
        f"- [{receipt['id']}] {receipt['tool_name']} status={receipt['status']} "
        f"args_sha256={receipt['args_sha256']} output_sha256={receipt['output_sha256']} "
        f"bytes={receipt['output_bytes']}"
        for receipt in receipts
    ]
    if len("\n".join([*lines, *receipt_lines])) <= max_chars:
        return "\n".join([*lines, *receipt_lines]), list(receipts)

    omission = "- ... older receipts omitted (context budget)"
    retained: list[str] = []
    retained_count = 0
    for line in reversed(receipt_lines):
        candidate = [*lines, omission, line, *retained]
        if len("\n".join(candidate)) > max_chars:
            break
        retained.insert(0, line)
        retained_count += 1
    rendered = "\n".join([*lines, omission, *retained])
    if len(rendered) > max_chars:
        return f"{rendered[: max(0, max_chars - 4)]}\n...", []
    visible = receipts[-retained_count:] if retained_count else []
    return rendered, list(visible)
