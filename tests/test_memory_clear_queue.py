from __future__ import annotations

from typing import Any

from deerflow.agents.memory.backends.deermem import DeerMemManager
from deerflow.agents.memory.backends.mem0.config import Mem0Config
from deerflow.agents.memory.backends.mem0.mem0_manager import Mem0MemoryManager


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str | None, bool]] = []

    def cancel_by_agent(
        self,
        agent_name: str | None = None,
        *,
        user_id: str | None = None,
        all_agents: bool = False,
        all_users: bool = False,
    ) -> int:
        assert all_agents is False
        self.calls.append((agent_name, user_id, all_users))
        return 0


def test_deermem_clear_cancels_all_pending_users_before_and_after_clear(
    monkeypatch,
) -> None:
    queue = _RecordingQueue()
    durable_calls: list[str | None] = []

    def clear_memory_data(agent_name: str | None) -> dict[str, Any]:
        assert queue.calls == [("agent-a", None, True)]
        durable_calls.append(agent_name)
        return {"success": True}

    monkeypatch.setattr(
        "deerflow.agents.memory.queue.get_memory_queue",
        lambda: queue,
    )
    monkeypatch.setattr(
        "deerflow.agents.memory.updater.clear_memory_data",
        clear_memory_data,
    )

    assert DeerMemManager().clear_memory("agent-a") == {"success": True}
    assert durable_calls == ["agent-a"]
    assert queue.calls == [
        ("agent-a", None, True),
        ("agent-a", None, True),
    ]


def test_mem0_clear_cancels_legacy_and_default_user_pending_writes(
    monkeypatch,
) -> None:
    queue = _RecordingQueue()

    class Client:
        def clear(self, *, user_id: str, agent_name: str | None) -> dict[str, Any]:
            assert queue.calls == [
                ("agent-a", None, False),
                ("agent-a", "tenant-1", False),
            ]
            assert user_id == "tenant-1"
            assert agent_name == "agent-a"
            return {"success": True}

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "deerflow.agents.memory.queue.get_memory_queue",
        lambda: queue,
    )
    manager = Mem0MemoryManager(
        Mem0Config(default_user_id="tenant-1"),
        client=Client(),  # type: ignore[arg-type]
    )

    assert manager.clear_memory("agent-a") == {"success": True}
    assert queue.calls == [
        ("agent-a", None, False),
        ("agent-a", "tenant-1", False),
        ("agent-a", None, False),
        ("agent-a", "tenant-1", False),
    ]
