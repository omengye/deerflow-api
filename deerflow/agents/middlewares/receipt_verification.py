"""Verify subagent report citations against runtime-stamped tool receipts."""

from __future__ import annotations

import re
from typing import TypedDict

from deerflow.agents.middlewares.tool_receipt import ToolReceipt, parse_citations

VERDICT_SOURCE = "receipt_citations"
VERDICT_REQUIREMENT = "cited_ids_in_execution_record"
_ACTION_VERB_RE = re.compile(
    r"\b(wrote|written|created|saved|generated|ran|executed|uploaded|downloaded"
    r"|deleted|modified|updated|installed|deployed|fetched|built|compiled"
    r"|produced|exported|fixed|added|changed|removed|implemented|patched"
    r"|refactored|renamed|moved|merged|committed|edited|replaced|tested"
    r"|verified|cleaned|configured)\b",
    re.IGNORECASE,
)
_CJK_ACTION_VERB_RE = re.compile(
    r"创建|生成|写入|保存|修改|更新|删除|运行|执行|安装|部署|上传|下载"
    r"|修复|添加|新增|编写|编译|构建|导出|测试|提交|移动|重命名|配置|替换|清理"
)
_FILE_PATH_RE = re.compile(
    r"(?:/[\w.\-]+){2,}|\b[\w.\-]+\."
    r"(?:py|md|txt|json|ya?ml|csv|html|js|ts|sh|log|pdf|png|jpe?g)\b"
)
_NONTRIVIAL_REPORT_MIN_CHARS = 240


class CitationFailure(TypedDict):
    id: str
    reason: str


class ReceiptVerdict(TypedDict):
    source: str
    requirement: str
    citation_resolved: bool
    cited: list[str]
    resolved: list[str]
    failed: list[CitationFailure]
    unknown: list[str]
    no_citation_claims: bool


def _has_action_claims(report_text: str) -> bool:
    return bool(
        _ACTION_VERB_RE.search(report_text)
        or _CJK_ACTION_VERB_RE.search(report_text)
        or _FILE_PATH_RE.search(report_text)
    )


def verify_receipt_citations(
    report_text: str,
    receipts: list[ToolReceipt],
) -> ReceiptVerdict:
    by_id = {receipt["id"]: receipt for receipt in receipts}
    cited: list[str] = []
    resolved: list[str] = []
    failed: list[CitationFailure] = []
    unknown: list[str] = []
    for rid, anchor in parse_citations(report_text):
        cited.append(rid)
        receipt = by_id.get(rid)
        if receipt is None:
            unknown.append(rid)
        elif receipt["status"] != "success":
            failed.append({"id": rid, "reason": f"receipt status={receipt['status']}"})
        elif anchor is not None and anchor != receipt["tool_name"]:
            failed.append(
                {
                    "id": rid,
                    "reason": (
                        f"anchor mismatch: cited as {anchor}, receipt {rid} is "
                        f"{receipt['tool_name']}"
                    ),
                }
            )
        else:
            resolved.append(rid)

    no_citation_claims = not cited and (
        _has_action_claims(report_text)
        or bool(receipts)
        and len(report_text.strip()) >= _NONTRIVIAL_REPORT_MIN_CHARS
    )
    citation_resolved = not failed and not unknown if cited else not no_citation_claims
    return ReceiptVerdict(
        source=VERDICT_SOURCE,
        requirement=VERDICT_REQUIREMENT,
        citation_resolved=citation_resolved,
        cited=cited,
        resolved=resolved,
        failed=failed,
        unknown=unknown,
        no_citation_claims=no_citation_claims,
    )


def render_citation_verdict(verdict: ReceiptVerdict) -> str:
    if verdict["no_citation_claims"]:
        return "Citations: UNVERIFIED — action claims lack receipt citations."
    if not verdict["cited"]:
        return ""
    parts = [f"{len(verdict['resolved'])} resolved"]
    if verdict["failed"]:
        parts.append(f"{len(verdict['failed'])} failed")
    if verdict["unknown"]:
        parts.append(f"{len(verdict['unknown'])} unknown")
    return (
        f"Citations: {', '.join(parts)} — execution evidence only; "
        "this does not validate claim correctness."
    )
