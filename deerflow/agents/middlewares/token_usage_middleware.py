"""Middleware for logging LLM token usage."""

import logging
from collections.abc import Callable
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)


class TokenUsageMiddleware(AgentMiddleware):
    """Logs token usage from model response usage_metadata and optionally streams it."""

    def __init__(self, stream_callback: Callable[[dict[str, Any]], None] | None = None):
        super().__init__()
        self.stream_callback = stream_callback

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._log_usage(state)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._log_usage(state)

    def _log_usage(self, state: AgentState) -> None:
        messages = state.get("messages", [])
        if not messages:
            return None
        last = messages[-1]
        usage = getattr(last, "usage_metadata", None)
        if usage:
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            total_tokens = usage.get("total_tokens", 0)
            logger.info(
                "LLM token usage: input=%s output=%s total=%s",
                input_tokens,
                output_tokens,
                total_tokens,
            )
            if self.stream_callback is not None:
                try:
                    self.stream_callback({
                        "type": "token_usage",
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": total_tokens,
                    })
                except Exception:
                    logger.debug("stream_callback raised on token_usage, ignoring", exc_info=True)
        return None
