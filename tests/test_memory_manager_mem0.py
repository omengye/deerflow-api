from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from deerflow.agents.memory.backends.mem0.client import Mem0HttpClient
from deerflow.agents.memory.backends.mem0.config import Mem0Config
from deerflow.agents.memory.backends.mem0.mem0_manager import (
    Mem0MemoryManager,
    _format_entries,
)
from deerflow.agents.memory.backends.mem0.message_filtering import to_mem0_messages
from deerflow.agents.memory.manager import (
    MemoryManager,
    MemoryOperationUnsupported,
    _construct_manager,
    get_memory_manager,
    memory_manager_lease,
    reset_memory_manager,
    validate_memory_manager_config,
)
from deerflow.config.memory_config import MemoryConfig


def test_legacy_memory_config_is_copied_into_backend_config() -> None:
    config = MemoryConfig(storage_path="legacy.json", retrieval_top_k=5)
    assert config.manager_class == "deermem"
    assert config.backend_config["storage_path"] == "legacy.json"
    assert config.backend_config["retrieval_top_k"] == 5


def test_nested_deermem_config_populates_legacy_properties() -> None:
    config = MemoryConfig(
        manager_class="deermem",
        backend_config={"storage_path": "nested.json", "retrieval_top_k": 3},
    )
    assert config.storage_path == "nested.json"
    assert config.retrieval_top_k == 3


def test_unchanged_memory_config_reload_keeps_stateful_manager(
    monkeypatch,
) -> None:
    import deerflow.config.memory_config as memory_config_module

    current = memory_config_module.get_memory_config()
    reset_calls: list[bool] = []
    monkeypatch.setattr(
        "deerflow.agents.memory.manager.reset_memory_manager",
        lambda: reset_calls.append(True),
    )

    memory_config_module.load_memory_config_from_dict(current.model_dump())

    assert reset_calls == []


def test_unknown_short_manager_fails_fast() -> None:
    with pytest.raises(ValidationError, match="Unknown memory manager"):
        MemoryConfig(manager_class="missing")


class _CustomMemoryManager(MemoryManager):
    def __init__(self, config: MemoryConfig) -> None:
        self.config = config

    def add(self, **_kwargs: Any) -> bool:
        return True

    def get_context(self, **_kwargs: Any) -> str:
        return "custom"

    def search(self, query: str, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"memory": query}]


class _ContextOnlyMemoryManager(MemoryManager):
    def add(self, **_kwargs: Any) -> bool:
        return True

    def get_context(self, **_kwargs: Any) -> str:
        return "custom"


class _OptionalConfigMemoryManager(_CustomMemoryManager):
    def __init__(self, config: MemoryConfig | None = None) -> None:
        self.config = config


class _KeywordConfigMemoryManager(_CustomMemoryManager):
    def __init__(self, *, config: MemoryConfig) -> None:
        self.config = config


class _ClosingMemoryManager(_CustomMemoryManager):
    closed = 0

    def close(self) -> None:
        type(self).closed += 1


def test_custom_manager_dotted_path_is_constructed_through_factory() -> None:
    config = MemoryConfig(
        manager_class=f"{__name__}:_CustomMemoryManager",
        mode="tool",
    )
    manager = _construct_manager(config)
    assert isinstance(manager, _CustomMemoryManager)
    assert manager.config is config
    assert manager.search("hello") == [{"memory": "hello"}]


@pytest.mark.parametrize(
    "manager_class",
    [_OptionalConfigMemoryManager, _KeywordConfigMemoryManager],
)
def test_custom_manager_optional_or_keyword_config_receives_candidate(
    manager_class: type[MemoryManager],
) -> None:
    config = MemoryConfig(
        manager_class=f"{__name__}:{manager_class.__name__}",
        backend_config={"custom": "value"},
    )

    manager = _construct_manager(config)

    assert manager.config is config


def test_custom_backend_keys_do_not_populate_deermem_legacy_fields() -> None:
    config = MemoryConfig(
        manager_class=f"{__name__}:_CustomMemoryManager",
        backend_config={
            "model_name": "external-backend-model",
            "storage_path": "backend-owned-path",
        },
    )

    assert config.model_name is None
    assert config.storage_path == ""
    assert config.backend_config == {
        "model_name": "external-backend-model",
        "storage_path": "backend-owned-path",
    }


def test_custom_manager_contract_rejects_non_manager_class() -> None:
    with pytest.raises(TypeError, match="must subclass MemoryManager"):
        _construct_manager(MemoryConfig(manager_class="builtins:dict"))


def test_failed_manager_replacement_keeps_previous_instance() -> None:
    good_config = MemoryConfig(
        manager_class=f"{__name__}:_CustomMemoryManager"
    )
    reset_memory_manager()
    try:
        previous = get_memory_manager(good_config)
        with pytest.raises(TypeError, match="must subclass MemoryManager"):
            get_memory_manager(MemoryConfig(manager_class="builtins:dict"))
        assert get_memory_manager(good_config) is previous
    finally:
        reset_memory_manager()


def test_leased_manager_is_closed_only_after_active_operation_finishes() -> None:
    config = MemoryConfig(manager_class=f"{__name__}:_ClosingMemoryManager")
    _ClosingMemoryManager.closed = 0
    reset_memory_manager()
    try:
        with memory_manager_lease(config) as manager:
            assert isinstance(manager, _ClosingMemoryManager)
            reset_memory_manager()
            assert _ClosingMemoryManager.closed == 0
        assert _ClosingMemoryManager.closed == 1
    finally:
        reset_memory_manager()


def test_tool_mode_rejects_manager_without_structured_search() -> None:
    config = MemoryConfig(
        manager_class=f"{__name__}:_ContextOnlyMemoryManager",
        mode="tool",
    )
    with pytest.raises(ValueError, match="implements search"):
        validate_memory_manager_config(config)


def test_deermem_tool_mode_requires_retrieval() -> None:
    config = MemoryConfig(
        manager_class="deermem",
        mode="tool",
        backend_config={"retrieval_enabled": False},
    )
    with pytest.raises(ValueError, match="retrieval_enabled=true"):
        validate_memory_manager_config(config)


def test_deermem_probe_requires_sqlite_fts5(
    tmp_path,
    monkeypatch,
) -> None:
    from deerflow.agents.memory.backends.deermem import DeerMemManager

    config = MemoryConfig(
        manager_class="deermem",
        backend_config={
            "storage_path": str(tmp_path / "memory.json"),
            "retrieval_enabled": True,
            "retrieval_index_path": str(tmp_path / "memory-fts5.sqlite3"),
        },
    )

    def missing_fts5(*_args, **_kwargs):
        raise __import__("sqlite3").OperationalError("no such module: fts5")

    monkeypatch.setattr("deerflow.agents.memory.backends.deermem.sqlite3.connect", missing_fts5)
    with pytest.raises(RuntimeError, match="FTS5 support"):
        DeerMemManager(config).probe()


def test_deermem_probe_rejects_index_directory(tmp_path) -> None:
    from deerflow.agents.memory.backends.deermem import DeerMemManager

    index_path = tmp_path / "memory-fts5.sqlite3"
    index_path.mkdir()
    config = MemoryConfig(
        manager_class="deermem",
        backend_config={
            "storage_path": str(tmp_path / "memory.json"),
            "retrieval_enabled": True,
            "retrieval_index_path": str(index_path),
        },
    )

    with pytest.raises(OSError, match="is not a file"):
        DeerMemManager(config).probe()


def test_mem0_requires_https_unless_explicitly_allowed() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        Mem0Config(base_url="http://mem0.internal")
    assert Mem0Config(
        base_url="http://localhost:8080", allow_insecure_http=True
    ).base_url == "http://localhost:8080"


def test_mem0_api_key_comes_only_from_named_environment(monkeypatch) -> None:
    monkeypatch.delenv("TEST_MEM0_KEY", raising=False)
    with pytest.raises(ValueError, match="TEST_MEM0_KEY"):
        Mem0HttpClient(Mem0Config(api_key_env="TEST_MEM0_KEY"))


def test_mem0_http_endpoints_and_identity_mapping(monkeypatch) -> None:
    monkeypatch.setenv("TEST_MEM0_KEY", "secret")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/search/"):
            return httpx.Response(200, json={"results": [{"memory": "prefers Python", "score": 0.9}]})
        return httpx.Response(200, json={"results": []})

    raw = httpx.Client(
        base_url="https://api.mem0.test", transport=httpx.MockTransport(handle)
    )
    config = Mem0Config(
        api_key_env="TEST_MEM0_KEY", base_url="https://api.mem0.test"
    )
    client = Mem0HttpClient(config, http_client=raw)
    client.add(
        [{"role": "user", "content": "hello"}],
        user_id="user-1",
        agent_name="lead",
        thread_id="thread-1",
    )
    results = client.search(
        "Python",
        user_id="user-1",
        agent_name="lead",
        thread_id="thread-1",
    )

    assert [request.url.path for request in requests] == [
        "/v3/memories/add/",
        "/v3/memories/search/",
    ]
    payload = __import__("json").loads(requests[0].content)
    assert payload["user_id"] == "user-1"
    assert payload["agent_id"] == "lead"
    assert payload["run_id"] == "thread-1"
    assert results[0]["memory"] == "prefers Python"
    raw.close()


def test_mem0_context_truncates_only_at_entry_boundaries() -> None:
    context = _format_entries(
        [
            {"memory": "first complete entry", "score": 0.9},
            {"memory": "second entry must not be sliced", "score": 0.8},
        ],
        score_threshold=0.1,
        max_chars=25,
    )
    assert context == "- first complete entry"
    assert "second" not in context

    later_short_entry = _format_entries(
        [
            {"memory": "x" * 100, "score": 0.9},
            {"memory": "fits", "score": 0.8},
        ],
        score_threshold=0.1,
        max_chars=16,
    )
    assert later_short_entry == "- fits"


def test_mem0_message_filter_excludes_tools_and_task_scoped_data() -> None:
    task_message = HumanMessage(
        content="internal task detail",
        additional_kwargs={"_deerflow_task_scoped": True},
    )
    messages = [
        HumanMessage(content="remember this"),
        ToolMessage(content="tool output", tool_call_id="call-1"),
        task_message,
        AIMessage(content="understood"),
    ]
    assert to_mem0_messages(messages) == [
        {"role": "user", "content": "remember this"},
        {"role": "assistant", "content": "understood"},
    ]


def test_mem0_read_and_write_failure_policies() -> None:
    class BrokenClient:
        def search(self, *_args: Any, **_kwargs: Any):
            raise httpx.ConnectError("offline")

        def add(self, *_args: Any, **_kwargs: Any):
            raise httpx.ConnectError("offline")

        def close(self):
            pass

    open_manager = Mem0MemoryManager(
        Mem0Config(
            failure_policy={"read": "fail_open", "write": "log_and_drop"}
        ),
        client=BrokenClient(),
    )
    assert open_manager.get_context(query="hello") == ""
    assert not open_manager.add(
        messages=[HumanMessage(content="hello")], thread_id="thread"
    )

    closed_manager = Mem0MemoryManager(
        Mem0Config(failure_policy={"read": "fail_closed", "write": "raise"}),
        client=BrokenClient(),
    )
    with pytest.raises(httpx.ConnectError):
        closed_manager.get_context(query="hello")
    with pytest.raises(httpx.ConnectError):
        closed_manager.add(
            messages=[HumanMessage(content="hello")], thread_id="thread"
        )


def test_mem0_fail_open_logs_do_not_expose_backend_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "backend-secret-in-exception"

    class BrokenClient:
        def search(self, *_args: Any, **_kwargs: Any):
            raise RuntimeError(secret)

        def add(self, *_args: Any, **_kwargs: Any):
            raise RuntimeError(secret)

        def close(self):
            pass

    manager = Mem0MemoryManager(Mem0Config(), client=BrokenClient())
    with caplog.at_level("WARNING"):
        assert manager.get_context(query="hello") == ""
        assert not manager.add(
            messages=[HumanMessage(content="hello")], thread_id="thread"
        )

    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text


def test_mem0_explicitly_rejects_unsupported_fact_crud() -> None:
    manager = object.__new__(Mem0MemoryManager)
    with pytest.raises(MemoryOperationUnsupported):
        manager.create_fact(content="fact")


def test_memory_search_tool_propagates_runtime_identity(monkeypatch) -> None:
    from deerflow.tools.builtins.memory_tool import memory_search_tool

    captured: dict[str, Any] = {}

    class SearchManager:
        def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            captured.update({"query": query, **kwargs})
            return [{"id": "m1", "memory": "prefers Python"}]

    class Runtime:
        def __init__(self) -> None:
            self.context = {
                "thread_id": "thread-1",
                "user_id": "user-1",
                "agent_name": "lead",
            }
            self.config: dict[str, Any] = {}

    monkeypatch.setattr(
        "deerflow.agents.memory.manager.get_memory_manager",
        lambda: SearchManager(),
    )
    result = __import__("json").loads(
        memory_search_tool.func(runtime=Runtime(), query="Python", limit=999)
    )

    assert result["count"] == 1
    assert captured == {
        "query": "Python",
        "thread_id": "thread-1",
        "agent_name": "lead",
        "user_id": "user-1",
        "limit": 50,
    }


def test_memory_search_tool_redacts_backend_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from deerflow.tools.builtins.memory_tool import memory_search_tool

    secret = "custom-manager-secret"

    class BrokenManager:
        def search(self, *_args: Any, **_kwargs: Any):
            raise RuntimeError(secret)

    runtime = SimpleNamespace(context={}, config={})
    monkeypatch.setattr(
        "deerflow.agents.memory.manager.get_memory_manager",
        lambda: BrokenManager(),
    )

    with caplog.at_level("WARNING"):
        result = memory_search_tool.func(
            runtime=runtime,
            query="anything",
            limit=10,
        )

    assert secret not in result
    assert secret not in caplog.text
    assert "RuntimeError" in result
    assert "RuntimeError" in caplog.text


def test_tool_mode_registers_memory_search_only_when_selected(monkeypatch) -> None:
    import deerflow.tools.tools as tool_loader

    config = SimpleNamespace(
        tools=[],
        memory=MemoryConfig(enabled=True, mode="tool"),
        skill_evolution=SimpleNamespace(enabled=False),
        models=[],
    )
    monkeypatch.setattr(tool_loader, "get_app_config", lambda: config)
    monkeypatch.setattr(tool_loader, "is_host_bash_allowed", lambda _config: True)
    monkeypatch.setattr(
        "deerflow.config.acp_config.get_acp_agents", dict
    )

    tool_names = {
        tool.name for tool in tool_loader.get_available_tools(include_mcp=False)
    }
    assert "memory_search" in tool_names

    config.memory = MemoryConfig(enabled=True, mode="middleware")
    tool_names = {
        tool.name for tool in tool_loader.get_available_tools(include_mcp=False)
    }
    assert "memory_search" not in tool_names
