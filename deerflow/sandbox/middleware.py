import logging
from typing import NotRequired, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.runtime import Runtime

from deerflow.agents.thread_state import SandboxState, ThreadDataState
from deerflow.sandbox import get_sandbox_provider
from deerflow.sandbox.sandbox_provider import workspace_mount_path_from_thread_data

logger = logging.getLogger(__name__)


class SandboxMiddlewareState(AgentState):
    """Compatible with the `ThreadState` schema."""

    sandbox: NotRequired[SandboxState | None]
    thread_data: NotRequired[ThreadDataState | None]


class SandboxMiddleware(AgentMiddleware[SandboxMiddlewareState]):
    """Create a sandbox environment and assign it to an agent.

    Lifecycle Management:
    - With lazy_init=True (default): Sandbox is acquired on first tool call
    - With lazy_init=False: Sandbox is acquired on first agent invocation (before_agent)
    - Sandbox is reused across multiple turns within the same thread
    - Sandbox is NOT released after each agent call to avoid wasteful recreation
    - Cleanup happens at application shutdown via SandboxProvider.shutdown()
    """

    state_schema = SandboxMiddlewareState

    def __init__(self, lazy_init: bool = True):
        """Initialize sandbox middleware.

        Args:
            lazy_init: If True, defer sandbox acquisition until first tool call.
                      If False, acquire sandbox eagerly in before_agent().
                      Default is True for optimal performance.
        """
        super().__init__()
        self._lazy_init = lazy_init

    def _acquire_sandbox(
        self,
        thread_id: str,
        available_skills: list[str] | None,
        workspace_path: str | None,
    ) -> tuple[str, dict]:
        from deerflow.skills.projection import get_skill_projection

        projection = get_skill_projection(available_skills)
        provider = get_sandbox_provider()
        if workspace_path is None:
            sandbox_id = provider.acquire(
                thread_id,
                available_skills=available_skills,
            )
        else:
            sandbox_id = provider.acquire(
                thread_id,
                available_skills=available_skills,
                workspace_path=workspace_path,
            )
        logger.info(f"Acquiring sandbox {sandbox_id}")
        return sandbox_id, {
            "sandbox_id": sandbox_id,
            "skills_revision": projection.revision,
            "skills_path": str(projection.path),
            "available_skills": available_skills,
            "workspace_path": workspace_path,
        }

    @override
    def before_agent(self, state: SandboxMiddlewareState, runtime: Runtime) -> dict | None:
        # Skip acquisition if lazy_init is enabled
        if self._lazy_init:
            return super().before_agent(state, runtime)

        # Eager initialization (original behavior)
        if "sandbox" not in state or state["sandbox"] is None:
            thread_id = (runtime.context or {}).get("thread_id")
            if thread_id is None:
                return super().before_agent(state, runtime)
            raw_skills = (runtime.context or {}).get("available_skills")
            if raw_skills is None:
                raw_skills = (runtime.config or {}).get("metadata", {}).get("available_skills")
            available_skills = list(raw_skills) if raw_skills is not None else None
            workspace_path = workspace_mount_path_from_thread_data(
                state.get("thread_data")
            )
            sandbox_id, sandbox_state = self._acquire_sandbox(
                thread_id,
                available_skills,
                workspace_path,
            )
            logger.info(f"Assigned sandbox {sandbox_id} to thread {thread_id}")
            return {"sandbox": sandbox_state}
        return super().before_agent(state, runtime)

    @override
    def after_agent(self, state: SandboxMiddlewareState, runtime: Runtime) -> dict | None:
        sandbox = state.get("sandbox")
        if sandbox is not None:
            sandbox_id = sandbox["sandbox_id"]
            logger.info(f"Releasing sandbox {sandbox_id}")
            get_sandbox_provider().release(sandbox_id)
            return None

        if (runtime.context or {}).get("sandbox_id") is not None:
            sandbox_id = runtime.context.get("sandbox_id")
            logger.info(f"Releasing sandbox {sandbox_id} from context")
            get_sandbox_provider().release(sandbox_id)
            return None

        # No sandbox to release
        return super().after_agent(state, runtime)
