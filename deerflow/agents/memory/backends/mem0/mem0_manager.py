"""MemoryManager implementation backed by mem0's HTTP API."""

from __future__ import annotations

import html
import logging
from typing import Any

from deerflow.agents.memory.manager import MemoryManager
from deerflow.config.memory_config import MemoryConfig

from .client import Mem0HttpClient
from .config import Mem0Config
from .message_filtering import to_mem0_messages

logger = logging.getLogger(__name__)


class Mem0MemoryManager(MemoryManager):
    def __init__(
        self,
        config: Mem0Config,
        *,
        client: Mem0HttpClient | None = None,
    ) -> None:
        self.config = config
        self._client = client or Mem0HttpClient(config)

    @classmethod
    def validate_config(cls, config: MemoryConfig) -> None:
        Mem0Config.model_validate(config.backend_config)

    @classmethod
    def from_config(cls, config: MemoryConfig) -> Mem0MemoryManager:
        return cls(Mem0Config.model_validate(config.backend_config))

    def _user_id(self, user_id: str | None) -> str:
        return user_id or self.config.default_user_id

    def add(
        self,
        *,
        messages: list[Any],
        thread_id: str,
        agent_name: str | None = None,
        user_id: str | None = None,
        **metadata: Any,
    ) -> bool:
        payload = to_mem0_messages(messages)
        if not payload:
            return True
        try:
            self._client.add(
                payload,
                user_id=self._user_id(user_id),
                agent_name=agent_name,
                thread_id=thread_id,
            )
            return True
        except Exception as exc:
            if self.config.failure_policy.write == "raise":
                raise
            logger.warning(
                "mem0 memory write failed; dropping update (%s)",
                type(exc).__name__,
            )
            return False

    def search(
        self,
        query: str,
        *,
        thread_id: str | None = None,
        agent_name: str | None = None,
        user_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._client.search(
            query,
            user_id=self._user_id(user_id),
            agent_name=agent_name,
            thread_id=thread_id,
            top_k=(
                min(max(1, limit), self.config.top_k)
                if limit is not None
                else self.config.top_k
            ),
            threshold=self.config.score_threshold,
        )

    def get_context(
        self,
        *,
        query: str = "",
        thread_id: str | None = None,
        agent_name: str | None = None,
        user_id: str | None = None,
    ) -> str:
        try:
            if query:
                entries = self.search(
                    query,
                    thread_id=thread_id,
                    agent_name=agent_name,
                    user_id=user_id,
                )
            else:
                entries = self._client.list_memories(
                    user_id=self._user_id(user_id),
                    agent_name=agent_name,
                    thread_id=thread_id,
                    limit=self.config.top_k,
                )
            return _format_entries(
                entries,
                score_threshold=self.config.score_threshold,
                max_chars=self.config.max_injection_chars,
            )
        except Exception as exc:
            if self.config.failure_policy.read == "fail_closed":
                raise
            logger.warning(
                "mem0 memory read failed; continuing without memory (%s)",
                type(exc).__name__,
            )
            return ""

    def warm(self) -> None:
        try:
            self.probe()
        except Exception as exc:
            if self.config.startup_policy == "fail_fast":
                raise
            logger.warning(
                "mem0 startup check failed; continuing best-effort (%s)",
                type(exc).__name__,
            )

    def probe(self) -> None:
        """Strict connectivity check used by the Admin test endpoint."""
        self._client.ping(user_id=self.config.default_user_id)

    def clear_memory(self, agent_name: str | None = None) -> dict[str, Any]:
        # Queued contexts with no explicit user id resolve to default_user_id
        # at write time. Cancel both spellings of that same mem0 scope.
        self.cancel_by_agent(agent_name)
        self.cancel_by_agent(agent_name, user_id=self.config.default_user_id)
        result = self._client.clear(
            user_id=self.config.default_user_id,
            agent_name=agent_name,
        )
        self.cancel_by_agent(agent_name)
        self.cancel_by_agent(agent_name, user_id=self.config.default_user_id)
        return result if isinstance(result, dict) else {"success": True}

    def close(self) -> None:
        self._client.close()


def _entry_text(entry: dict[str, Any]) -> str:
    for key in ("memory", "text", "content"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _entry_score(entry: dict[str, Any]) -> float | None:
    value = entry.get("score")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _format_entries(
    entries: list[dict[str, Any]],
    *,
    score_threshold: float,
    max_chars: int,
) -> str:
    """Truncate only between entries so no memory fragment is injected."""
    lines: list[str] = []
    used = 0
    seen: set[str] = set()
    for entry in entries:
        entry_id = str(entry.get("id") or "")
        if entry_id and entry_id in seen:
            continue
        if entry_id:
            seen.add(entry_id)
        score = _entry_score(entry)
        if score is not None and score < score_threshold:
            continue
        text = _entry_text(entry)
        if not text:
            continue
        line = f"- {html.escape(text, quote=False)}"
        added = len(line) + (1 if lines else 0)
        if used + added > max_chars:
            # Keep scanning: an oversized entry must not prevent a shorter
            # later entry from fitting in the remaining budget.
            continue
        lines.append(line)
        used += added
    return "\n".join(lines)
