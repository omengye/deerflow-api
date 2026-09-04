"""Backend-neutral memory manager contract and singleton factory."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from deerflow.config.memory_config import MemoryConfig, get_memory_config

logger = logging.getLogger(__name__)


class MemoryOperationUnsupported(NotImplementedError):
    """The active backend does not implement an optional management API."""


class MemoryManager(ABC):
    """Minimal contract required by DeerFlow's memory middleware."""

    @abstractmethod
    def add(
        self,
        *,
        messages: list[Any],
        thread_id: str,
        agent_name: str | None = None,
        user_id: str | None = None,
        **metadata: Any,
    ) -> bool:
        """Persist durable information extracted from a completed conversation."""

    async def aadd(self, **kwargs: Any) -> bool:
        return await asyncio.to_thread(self.add, **kwargs)

    @abstractmethod
    def get_context(
        self,
        *,
        query: str = "",
        thread_id: str | None = None,
        agent_name: str | None = None,
        user_id: str | None = None,
    ) -> str:
        """Return prompt-ready descriptive context, without XML boundaries."""

    async def aget_context(self, **kwargs: Any) -> str:
        return await asyncio.to_thread(self.get_context, **kwargs)

    def search(
        self,
        query: str,
        *,
        thread_id: str | None = None,
        agent_name: str | None = None,
        user_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return structured search results for tool-driven memory mode."""
        raise MemoryOperationUnsupported("search is not supported by this backend")

    def warm(self) -> None:
        """Validate connectivity and eagerly initialize backend resources."""

    def probe(self) -> None:
        """Strictly verify backend readiness without applying failure policy."""
        self.warm()

    def shutdown_flush(self) -> bool:
        """Flush queued writes before process shutdown, within the configured limit."""
        from deerflow.agents.memory.queue import get_memory_queue

        timeout = get_memory_config().shutdown_flush_timeout_seconds
        return get_memory_queue().flush(timeout_seconds=timeout)

    def close(self) -> None:
        """Release backend resources."""

    def cache_signature(self, agent_name: str | None = None) -> str | None:
        """Return a signature only for memory embedded in a static prompt."""
        return None

    def get_memory(self, agent_name: str | None = None) -> dict[str, Any]:
        raise MemoryOperationUnsupported("get_memory is not supported by this backend")

    def export_memory(self, agent_name: str | None = None) -> dict[str, Any]:
        return self.get_memory(agent_name)

    def import_memory(
        self, memory_data: dict[str, Any], agent_name: str | None = None
    ) -> dict[str, Any]:
        raise MemoryOperationUnsupported("import_memory is not supported by this backend")

    def reload_memory(self, agent_name: str | None = None) -> dict[str, Any]:
        raise MemoryOperationUnsupported("reload_memory is not supported by this backend")

    def clear_memory(self, agent_name: str | None = None) -> dict[str, Any]:
        raise MemoryOperationUnsupported("clear_memory is not supported by this backend")

    def cancel_by_agent(
        self,
        agent_name: str | None = None,
        *,
        user_id: str | None = None,
        all_agents: bool = False,
        all_users: bool = False,
    ) -> int:
        """Cancel memory writes that have not started processing yet."""
        from deerflow.agents.memory.queue import get_memory_queue

        return get_memory_queue().cancel_by_agent(
            agent_name,
            user_id=user_id,
            all_agents=all_agents,
            all_users=all_users,
        )

    def create_fact(self, **kwargs: Any) -> dict[str, Any]:
        raise MemoryOperationUnsupported("fact CRUD is not supported by this backend")

    def update_fact(self, **kwargs: Any) -> dict[str, Any]:
        raise MemoryOperationUnsupported("fact CRUD is not supported by this backend")

    def delete_fact(self, fact_id: str, **kwargs: Any) -> dict[str, Any]:
        raise MemoryOperationUnsupported("fact CRUD is not supported by this backend")


def _resolve_manager_class(name: str) -> type[MemoryManager]:
    if name == "deermem":
        from deerflow.agents.memory.backends.deermem import DeerMemManager

        return DeerMemManager
    if name == "mem0":
        from deerflow.agents.memory.backends.mem0 import Mem0MemoryManager

        return Mem0MemoryManager

    if ":" in name:
        module_name, class_name = name.rsplit(":", 1)
    else:
        module_name, class_name = name.rsplit(".", 1)
    candidate = getattr(importlib.import_module(module_name), class_name)
    if not isinstance(candidate, type) or not issubclass(candidate, MemoryManager):
        raise TypeError(
            f"Configured memory manager {name!r} must subclass MemoryManager"
        )
    return candidate


def validate_memory_manager_config(config: MemoryConfig) -> None:
    """Resolve the configured class without opening network connections."""
    manager_class = _resolve_manager_class(config.manager_class)
    if config.mode == "tool" and manager_class.search is MemoryManager.search:
        raise ValueError(
            f"memory.mode='tool' requires a manager that implements search(); "
            f"{manager_class.__name__} does not"
        )
    validator = getattr(manager_class, "validate_config", None)
    if callable(validator):
        validator(config)


def probe_memory_manager_config(config: MemoryConfig) -> None:
    """Strictly probe an isolated candidate without replacing the singleton."""
    validate_memory_manager_config(config)
    manager = _construct_manager(config)
    try:
        manager.probe()
    finally:
        try:
            manager.close()
        except Exception as exc:
            # Custom backends can include credentials in exception messages.
            # The Admin probe path must never copy those messages into logs.
            logger.warning(
                "Error closing probed memory manager (%s)",
                type(exc).__name__,
            )


def _construct_manager(config: MemoryConfig) -> MemoryManager:
    manager_class = _resolve_manager_class(config.manager_class)
    factory = getattr(manager_class, "from_config", None)
    if callable(factory):
        manager = factory(config)
    else:
        signature = inspect.signature(manager_class)
        constructor = cast(Any, manager_class)
        parameters = signature.parameters
        keyword_name = next(
            (
                name
                for name in ("config", "memory_config")
                if name in parameters
                and parameters[name].kind is not inspect.Parameter.POSITIONAL_ONLY
            ),
            None,
        )
        required_positional = [
            parameter
            for parameter in parameters.values()
            if parameter.kind
            in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
            and parameter.default is inspect.Parameter.empty
        ]
        if keyword_name is not None:
            signature.bind(**{keyword_name: config})
            manager = constructor(**{keyword_name: config})
        elif required_positional:
            signature.bind(config)
            manager = constructor(config)
        else:
            # Preserve compatibility with managers whose optional positional
            # argument is unrelated (for example an injected HTTP client).
            signature.bind()
            manager = constructor()
    if not isinstance(manager, MemoryManager):
        raise TypeError(
            f"Configured memory manager {config.manager_class!r} did not return MemoryManager"
        )
    if config.mode == "tool" and type(manager).search is MemoryManager.search:
        raise ValueError(
            f"memory.mode='tool' requires a manager that implements search(); "
            f"{type(manager).__name__} does not"
        )
    return manager


def _config_key(config: MemoryConfig) -> str:
    payload = json.dumps(
        config.model_dump(), sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_manager: MemoryManager | None = None
_manager_key: str | None = None
_manager_lock = threading.RLock()
_manager_leases: dict[int, int] = {}
_retired_managers: dict[int, MemoryManager] = {}


def _close_manager(manager: MemoryManager, *, context: str) -> None:
    try:
        manager.close()
    except Exception as exc:
        logger.warning(
            "Error closing %s memory manager (%s)",
            context,
            type(exc).__name__,
        )


def _retire_manager_locked(manager: MemoryManager) -> MemoryManager | None:
    identity = id(manager)
    if _manager_leases.get(identity, 0) > 0:
        _retired_managers[identity] = manager
        return None
    return manager


def get_memory_manager(config: MemoryConfig | None = None) -> MemoryManager:
    """Return the active manager, failing fast for unknown backends."""
    global _manager, _manager_key
    selected = config or get_memory_config()
    key = _config_key(selected)
    close_previous: MemoryManager | None = None
    with _manager_lock:
        if _manager is None or _manager_key != key:
            # Construct first.  A broken replacement must not close and strand
            # the currently healthy manager.
            replacement = _construct_manager(selected)
            previous = _manager
            _manager = replacement
            _manager_key = key
            if previous is not None:
                close_previous = _retire_manager_locked(previous)
        selected_manager = _manager
    if close_previous is not None:
        _close_manager(close_previous, context="replaced")
    assert selected_manager is not None
    return selected_manager


@contextmanager
def memory_manager_lease(config: MemoryConfig | None = None) -> Iterator[MemoryManager]:
    """Keep a manager open for the duration of one read or write operation."""
    with _manager_lock:
        manager = get_memory_manager() if config is None else get_memory_manager(config)
        identity = id(manager)
        _manager_leases[identity] = _manager_leases.get(identity, 0) + 1
    try:
        yield manager
    finally:
        close_retired: MemoryManager | None = None
        with _manager_lock:
            remaining = _manager_leases.get(identity, 1) - 1
            if remaining > 0:
                _manager_leases[identity] = remaining
            else:
                _manager_leases.pop(identity, None)
                close_retired = _retired_managers.pop(identity, None)
        if close_retired is not None:
            _close_manager(close_retired, context="retired")


def reset_memory_manager(*, close: bool = True) -> None:
    global _manager, _manager_key
    close_manager: MemoryManager | None = None
    with _manager_lock:
        manager = _manager
        _manager = None
        _manager_key = None
        if close and manager is not None:
            close_manager = _retire_manager_locked(manager)
    if close_manager is not None:
        _close_manager(close_manager, context="reset")
