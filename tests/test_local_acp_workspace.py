from pathlib import Path
from types import SimpleNamespace

from deerflow.agents.middlewares.thread_data_middleware import ThreadDataMiddleware
from deerflow.sandbox.tools import ensure_thread_directories_exist


def test_thread_data_middleware_binds_acp_workspace_and_keeps_internal_outputs(
    tmp_path: Path,
) -> None:
    internal_root = tmp_path / "deerflow-state"
    project = tmp_path / "client-project"
    project.mkdir()
    middleware = ThreadDataMiddleware(base_dir=str(internal_root), lazy_init=True)
    runtime = SimpleNamespace(
        context={
            "thread_id": "acp-session",
            "workspace_path": str(project),
        }
    )

    result = middleware.before_agent({}, runtime)

    assert result is not None
    thread_data = result["thread_data"]
    assert thread_data["thread_id"] == "acp-session"
    assert thread_data["workspace_path"] == str(project)
    assert thread_data["workspace_path_managed"] is False
    assert thread_data["uploads_path"] == str(
        internal_root / "threads" / "acp-session" / "user-data" / "uploads"
    )
    assert thread_data["outputs_path"] == str(
        internal_root / "threads" / "acp-session" / "user-data" / "outputs"
    )


def test_removed_acp_workspace_is_not_recreated(tmp_path: Path) -> None:
    internal_root = tmp_path / "deerflow-state"
    project = tmp_path / "client-project"
    project.mkdir()
    middleware = ThreadDataMiddleware(base_dir=str(internal_root), lazy_init=True)
    middleware_runtime = SimpleNamespace(
        context={"thread_id": "acp-session", "workspace_path": str(project)}
    )
    result = middleware.before_agent({}, middleware_runtime)
    assert result is not None
    project.rmdir()
    runtime = SimpleNamespace(
        context={"thread_id": "acp-session"},
        state={
            "sandbox": {"sandbox_id": "local"},
            "thread_data": result["thread_data"],
        },
    )

    ensure_thread_directories_exist(runtime)

    assert not project.exists()
    assert Path(result["thread_data"]["uploads_path"]).is_dir()
    assert Path(result["thread_data"]["outputs_path"]).is_dir()
