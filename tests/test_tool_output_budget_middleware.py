import os
from types import SimpleNamespace

from langchain_core.messages import ToolMessage

from deerflow.agents.middlewares.tool_error_handling_middleware import build_subagent_runtime_middlewares
from deerflow.agents.middlewares.tool_output_budget_middleware import ToolOutputBudgetMiddleware, _build_fallback
from deerflow.config.app_config import AppConfig
from deerflow.config.app_config import reset_app_config, set_app_config
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.config.tool_output_config import ToolOutputConfig


def _request(outputs_path: str | None = None) -> SimpleNamespace:
    thread_data = {"outputs_path": outputs_path} if outputs_path else None
    return SimpleNamespace(tool_call={"name": "bash", "id": "tc-1"}, runtime=SimpleNamespace(state={"thread_data": thread_data} if thread_data else {}))


def test_fallback_never_exceeds_max_chars() -> None:
    result = _build_fallback("x" * 10_000, tool_name="bash", max_chars=500, head_chars=250, tail_chars=100)

    assert len(result) <= 500
    assert "omitted from bash output" in result


def test_large_tool_output_externalized(tmp_path) -> None:
    config = ToolOutputConfig(externalize_min_chars=20, preview_head_chars=8, preview_tail_chars=4)
    middleware = ToolOutputBudgetMiddleware(config=config)
    message = ToolMessage(content="a" * 100, name="bash", tool_call_id="tc-1")

    result = middleware.wrap_tool_call(_request(str(tmp_path)), lambda _: message)

    assert isinstance(result, ToolMessage)
    assert result is not message
    assert "Full bash output saved to /mnt/user-data/outputs/.tool-results/bash-" in str(result.content)
    storage_dir = tmp_path / ".tool-results"
    files = os.listdir(storage_dir)
    assert len(files) == 1
    assert (storage_dir / files[0]).read_text(encoding="utf-8") == "a" * 100


def test_tool_output_budget_middleware_is_in_runtime_chain() -> None:
    app_config = AppConfig(sandbox=SandboxConfig(use="test"))
    set_app_config(app_config)
    try:
        middlewares = build_subagent_runtime_middlewares(lazy_init=False)
    finally:
        reset_app_config()

    assert any(isinstance(middleware, ToolOutputBudgetMiddleware) for middleware in middlewares)
