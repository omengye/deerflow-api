"""Helpers for resolving human-readable run names for tracing.

The root LangGraph run name surfaces in Langfuse (as ``trace_name``) and
LangSmith dashboards. Resolving it from runtime config — rather than
hard-coding ``"lead_agent"`` — lets callers tag traces with the specific
agent that produced them (e.g. ``feishu_bot``, ``digest_agent``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def resolve_root_run_name(config: Mapping[str, Any], assistant_id: str | None) -> str:
    """Pick a run name from runnable config, falling back to assistant_id.

    Resolution order:
      1. ``config["context"]["agent_name"]`` — LangGraph >= 0.6 layout.
      2. ``config["configurable"]["agent_name"]`` — legacy layout.
      3. ``assistant_id`` if non-empty.
      4. ``"lead_agent"``.
    """
    for container_name in ("context", "configurable"):
        container = config.get(container_name)
        if isinstance(container, Mapping):
            agent_name = container.get("agent_name")
            if isinstance(agent_name, str) and agent_name.strip():
                return agent_name
    return assistant_id or "lead_agent"
