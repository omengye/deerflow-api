"""After-agent hook for lightweight Skill evolution observation."""

from __future__ import annotations

import logging
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config
from langgraph.runtime import Runtime

from deerflow.config import get_app_config
from deerflow.skills.evolution.monitor import EvolutionMonitor
from deerflow.skills.evolution.signal import EvolutionSignalCollector, analyze_latest_turn
from deerflow.skills.evolution.store import FileEvolutionStore
from deerflow.skills.evolution.worker import get_evolution_worker

logger = logging.getLogger(__name__)


class EvolutionSignalMiddleware(AgentMiddleware[AgentState]):
    """Observe a completed turn, persist a signal, and return immediately."""

    state_schema = AgentState

    def __init__(self, store: FileEvolutionStore | None = None):
        super().__init__()
        self.store = store or FileEvolutionStore()
        self.collector = EvolutionSignalCollector(self.store)
        self.monitor = EvolutionMonitor(self.store)

    @staticmethod
    def _metadata(runtime: Runtime) -> tuple[str | None, str | None]:
        context = runtime.context or {}
        thread_id = context.get("thread_id")
        run_id = context.get("run_id")
        try:
            config = get_config()
            configurable = config.get("configurable", {})
            metadata = config.get("metadata", {})
            thread_id = thread_id or configurable.get("thread_id")
            run_id = run_id or configurable.get("run_id") or metadata.get("run_id")
        except Exception:
            pass
        return str(thread_id) if thread_id else None, str(run_id) if run_id else None

    @override
    def after_agent(self, state: AgentState, runtime: Runtime) -> dict | None:
        try:
            config = get_app_config().skill_evolution
            if not config.enabled:
                return None
            messages = list(state.get("messages", []))
            analysis = analyze_latest_turn(messages)
            if analysis is None:
                return None

            # Monitoring remains active when discovery is paused so published
            # revisions can finish their probation window safely.
            self.monitor.observe(analysis)
            thread_id, run_id = self._metadata(runtime)
            signal = self.collector.collect(analysis, thread_id=thread_id, run_id=run_id)
            if signal is not None:
                get_evolution_worker().enqueue(signal.id)
        except Exception:
            # Self-improvement must never change the outcome of the user task.
            logger.exception("Failed to collect Skill evolution signal")
        return None
