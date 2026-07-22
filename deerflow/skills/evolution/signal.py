"""Lightweight task-signal extraction for automatic Skill evolution."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from deerflow.config import get_app_config

from .models import EvolutionSignal
from .store import FileEvolutionStore, utc_now_iso


_SKILL_PATH_RE = re.compile(
    r"(?:^|[/\\])mnt[/\\]skills[/\\](public|custom)[/\\]([^/\\]+)[/\\]SKILL\.md(?:$|[?#])",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"(?i)\b(api[-_ ]?key|access[-_ ]?token|secret|password|authorization)\b\s*[:=]\s*([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)(?:[?#][^\s]*)?", re.IGNORECASE)
_LONG_TOKEN_RE = re.compile(r"\b[a-zA-Z0-9_\-]{32,}\b")
_FINGERPRINT_NOISE_RE = re.compile(r"[^\w\u3400-\u9fff]+", re.UNICODE)
_CORRECTION_FALLBACK_RE = re.compile(
    r"(?i)(?:\b(?:that(?:'s| is) (?:wrong|incorrect)|you misunderstood|try again|redo)\b|不对|你理解错了|你理解有误|重试|重新来|换一种|改用)"
)


def _extract_message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return " ".join(parts)
    return str(content)


def _detect_current_correction(message: Any) -> bool:
    # Lazy import avoids a package-initialization cycle: deerflow.agents primes
    # the lead agent, which itself installs EvolutionSignalMiddleware.
    try:
        from deerflow.agents.memory.message_processing import detect_correction

        return detect_correction([message])
    except ImportError:
        return bool(_CORRECTION_FALLBACK_RE.search(_extract_message_text(message)))


def sanitize_summary(value: str, *, limit: int) -> str:
    """Remove common credentials and high-cardinality URL/token material."""
    text = " ".join((value or "").split())
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _URL_QUERY_RE.sub(r"\1", text)
    text = _LONG_TOKEN_RE.sub("[REDACTED]", text)
    return text[:limit]


def task_fingerprint(user_text: str) -> str:
    """Build a stable-enough fingerprint without persisting raw user input."""
    normalized = sanitize_summary(user_text, limit=2_000).casefold()
    normalized = re.sub(r"\b\d+\b", "#", normalized)
    normalized = _FINGERPRINT_NOISE_RE.sub(" ", normalized)
    normalized = " ".join(normalized.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class TurnAnalysis:
    """Pure, non-persistent analysis of the latest completed user turn."""

    user_summary: str
    assistant_summary: str
    fingerprint: str
    tool_names: list[str] = field(default_factory=list)
    tool_count: int = 0
    tool_error_count: int = 0
    recovered_error_count: int = 0
    unresolved_error_count: int = 0
    correction: bool = False
    skills_used: list[dict[str, Any]] = field(default_factory=list)
    has_final_assistant: bool = False
    used_skill_manage: bool = False


def _turn_bounds(messages: list[Any]) -> tuple[int | None, int | None]:
    human_indices = [index for index, message in enumerate(messages) if getattr(message, "type", None) == "human"]
    if not human_indices:
        return None, None
    current = human_indices[-1]
    previous = human_indices[-2] if len(human_indices) > 1 else None
    return previous, current


def _tool_path(args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    for key in ("path", "file_path", "filename", "file"):
        value = args.get(key)
        if isinstance(value, str):
            return value
    return None


def _skill_reads(messages: list[Any], *, source: str) -> list[dict[str, Any]]:
    skills: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            if str(call.get("name") or "") not in {"read_file", "read_file_tool", "read", "view", "cat"}:
                continue
            path = _tool_path(call.get("args"))
            if not path:
                continue
            match = _SKILL_PATH_RE.search(path.replace("\\", "/"))
            if not match:
                continue
            scope, name = match.group(1).lower(), match.group(2)
            key = (scope, name, source)
            if key in seen:
                continue
            seen.add(key)
            skills.append({"name": name, "scope": scope, "source": source})
    return skills


def _is_tool_error(message: ToolMessage) -> bool:
    status = str(getattr(message, "status", "") or "").lower()
    if status == "error":
        return True
    content = _extract_message_text(message).strip().lower()
    return content.startswith(("error:", "tool error:", "failed:"))


def analyze_latest_turn(messages: list[Any]) -> TurnAnalysis | None:
    """Analyze only the latest completed user turn in a message history."""
    previous_start, current_start = _turn_bounds(messages)
    if current_start is None:
        return None

    current_turn = messages[current_start:]
    current_user = messages[current_start]
    final_assistants = [
        message
        for message in current_turn[1:]
        if isinstance(message, AIMessage) and not (getattr(message, "tool_calls", None) or []) and _extract_message_text(message).strip()
    ]
    if not final_assistants:
        return None

    tool_calls: list[dict[str, Any]] = []
    calls_by_id: dict[str, tuple[str, int]] = {}
    for index, message in enumerate(current_turn):
        for call in getattr(message, "tool_calls", None) or []:
            tool_calls.append(call)
            call_id = str(call.get("id") or "")
            if call_id:
                calls_by_id[call_id] = (str(call.get("name") or "unknown"), index)

    tool_results: list[tuple[int, str, bool]] = []
    for index, message in enumerate(current_turn):
        if not isinstance(message, ToolMessage):
            continue
        call_id = str(getattr(message, "tool_call_id", "") or "")
        tool_name = calls_by_id.get(call_id, (str(getattr(message, "name", "") or "unknown"), index))[0]
        tool_results.append((index, tool_name, _is_tool_error(message)))

    recovered = 0
    unresolved = 0
    for result_index, tool_name, is_error in tool_results:
        if not is_error:
            continue
        later_success = any(
            later_index > result_index and later_name == tool_name and not later_error
            for later_index, later_name, later_error in tool_results
        )
        if later_success:
            recovered += 1
        else:
            unresolved += 1

    user_text = _extract_message_text(current_user)
    correction = _detect_current_correction(current_user)
    skills = _skill_reads(current_turn, source="current_turn")
    if correction and previous_start is not None:
        skills.extend(_skill_reads(messages[previous_start:current_start], source="previous_turn"))

    tool_names = [str(call.get("name") or "unknown") for call in tool_calls]
    return TurnAnalysis(
        user_summary=sanitize_summary(user_text, limit=1_000),
        assistant_summary=sanitize_summary(_extract_message_text(final_assistants[-1]), limit=1_500),
        fingerprint=task_fingerprint(user_text),
        tool_names=tool_names,
        tool_count=len(tool_calls),
        tool_error_count=sum(1 for _, _, is_error in tool_results if is_error),
        recovered_error_count=recovered,
        unresolved_error_count=unresolved,
        correction=correction,
        skills_used=skills,
        has_final_assistant=True,
        used_skill_manage="skill_manage" in tool_names,
    )


class EvolutionSignalCollector:
    """Persist actionable observations without invoking an LLM."""

    def __init__(self, store: FileEvolutionStore | None = None):
        self.store = store or FileEvolutionStore()

    def _within_quota(self) -> bool:
        config = get_app_config().skill_evolution.discovery
        proposals = self.store.list_proposals()
        pending = sum(1 for proposal in proposals if proposal.status in {"generating", "validating", "pending_review", "publishing"})
        if pending >= config.max_pending_proposals:
            return False
        today = datetime.now(UTC).date()
        automatic_today = 0
        for proposal in proposals:
            if proposal.origin != "automatic":
                continue
            try:
                if datetime.fromisoformat(proposal.created_at).date() == today:
                    automatic_today += 1
            except ValueError:
                continue
        return automatic_today < config.max_daily_proposals

    def collect(
        self,
        analysis: TurnAnalysis,
        *,
        thread_id: str | None = None,
        run_id: str | None = None,
    ) -> EvolutionSignal | None:
        config = get_app_config().skill_evolution
        discovery = config.discovery
        if not config.enabled or not discovery.enabled or analysis.used_skill_manage or not analysis.has_final_assistant:
            return None

        recurrence_count, cooling_down = self.store.register_observation(
            fingerprint=analysis.fingerprint,
            summary=analysis.user_summary,
            window_days=discovery.repeat_window_days,
            cooldown_hours=discovery.cooldown_hours,
        )
        triggers: list[str] = []
        if analysis.correction:
            triggers.append("correction")
        if analysis.tool_count >= discovery.min_tool_calls:
            triggers.append("high_tool_usage")
        if analysis.recovered_error_count:
            triggers.append("error_recovery")
        if recurrence_count >= discovery.repeat_threshold:
            triggers.append("repeated_task")
        previous_skill = any(item.get("source") == "previous_turn" for item in analysis.skills_used)
        current_skill = any(item.get("source") == "current_turn" for item in analysis.skills_used)
        if (analysis.correction and previous_skill) or (analysis.unresolved_error_count and current_skill):
            triggers.append("skill_regression")

        if not triggers or cooling_down or not self._within_quota():
            return None

        now = utc_now_iso()
        signal = EvolutionSignal(
            id=f"s_{uuid.uuid4().hex}",
            status="pending",
            fingerprint=analysis.fingerprint,
            trigger_types=list(dict.fromkeys(triggers)),
            thread_id=thread_id,
            run_id=run_id,
            user_summary=analysis.user_summary,
            assistant_summary=analysis.assistant_summary,
            tool_names=analysis.tool_names,
            tool_count=analysis.tool_count,
            tool_error_count=analysis.tool_error_count,
            recovered_error_count=analysis.recovered_error_count,
            unresolved_error_count=analysis.unresolved_error_count,
            recurrence_count=recurrence_count,
            skills_used=analysis.skills_used,
            created_at=now,
            updated_at=now,
        )
        self.store.save_signal(signal)
        self.store.mark_observation_signaled(analysis.fingerprint)
        self.store.append_audit(
            actor="system",
            action="signal.created",
            details={"signal_id": signal.id, "triggers": signal.trigger_types, "thread_id": thread_id},
        )
        return signal
