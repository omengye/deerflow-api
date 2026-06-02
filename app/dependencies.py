"""Client manager — singleton DeerFlowClient with checkpointer lifecycle."""
import asyncio
import logging
import atexit
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.config import settings
from deerflow.runtime import DisconnectMode, MemoryStreamBridge, RunManager, RunRecord, RunStatus

logger = logging.getLogger(__name__)


_client_manager = None
_lock = threading.Lock()


def _rmtree_via_root_container(paths, thread_id: str) -> None:
    """Remove a thread directory containing root-owned files via a root container.

    The AIO sandbox runs as root, so it leaves root-owned files (the
    daemon-created workdir, image-init scaffolding) inside the bind-mounted
    thread directory that the backend process cannot delete. We mount the
    *parent* directory into a throwaway root container and ``rm -rf`` the
    target so the deletion runs with the same privileges that created the
    files. ``host_thread_dir`` is used so the Docker daemon resolves the mount
    source correctly even when the backend runs inside a container.
    """
    import os
    import subprocess

    host_thread_dir = paths.host_thread_dir(thread_id)
    parent = os.path.dirname(host_thread_dir)
    name = os.path.basename(host_thread_dir)
    if not parent or not name:
        raise RuntimeError(f"Refusing to clean up unexpected thread path: {host_thread_dir!r}")
    subprocess.run(
        ["docker", "run", "--rm", "-v", f"{parent}:/target", "alpine", "rm", "-rf", f"/target/{name}"],
        shell=False,
        capture_output=True,
        timeout=60,
        check=True,
    )


class ClientManager:
    """Manages the shared DeerFlowClient instance."""

    def __init__(self):
        self._client = None
        self._checkpointer = None
        self._sync_sqlite_conn: sqlite3.Connection | None = None
        self._storage_checked = False
        self._init_lock = asyncio.Lock()
        self._async_checkpointer = None
        self._async_checkpointer_cm = None
        self._async_init_lock = asyncio.Lock()
        self._async_client_lock = asyncio.Lock()
        self._client_map: dict[tuple[object, ...], Any] = {}  # config_key -> DeerFlowClient
        self._async_client_map: dict[tuple[object, ...], Any] = {}  # config_key -> DeerFlowClient with async checkpointer
        self._running_threads: set[str] = set()  # thread_ids currently running
        self._thread_lock = threading.Lock()
        self.run_manager = RunManager()
        self.stream_bridge = MemoryStreamBridge(queue_maxsize=512)
        self.scheduler_service = None
        self.feishu_channel = None

    async def startup(self):
        """Initialize the DeerFlowClient on startup."""
        from deerflow.config.app_config import get_app_config, reload_app_config
        from app.config import ensure_data_dirs

        ensure_data_dirs()
        if settings.auth_enabled and not settings.api_keys:
            raise RuntimeError("DEER_FLOW_AUTH_ENABLED is true but DEER_FLOW_API_KEYS is empty")

        if settings.config_path:
            reload_app_config(settings.config_path)

        try:
            config = get_app_config()
        except Exception as e:
            raise RuntimeError(
                f"Failed to load DeerFlow config: {e}\n"
                f"Set DEER_FLOW_CONFIG_PATH or place config.yaml in the project root."
            ) from e

        self._assert_storage_ready()

        if settings.scheduler_enabled:
            from deerflow.runtime.scheduler import SchedulerService, SchedulerStore

            scheduler_db_path = Path(settings.scheduler_db_path)
            if not scheduler_db_path.is_absolute():
                scheduler_db_path = Path(settings.config_path).parent / scheduler_db_path
            store = SchedulerStore(scheduler_db_path)
            self.scheduler_service = SchedulerService(
                store=store,
                manager=self,
                poll_interval_seconds=settings.scheduler_poll_interval_seconds,
                default_timezone=settings.scheduler_timezone,
            )
            await self.scheduler_service.start()

    def get_client(self, **overrides) -> Any:
        """Get or create a DeerFlowClient instance."""
        from deerflow.client import DeerFlowClient

        key = (
            settings.checkpointer_type,
            settings.model_name,
            settings.thinking_enabled,
            settings.subagent_enabled,
            settings.plan_mode,
            settings.max_concurrent_subagents,
            settings.recursion_limit,
            frozenset(overrides.items()),
        )

        if key not in self._client_map:
            kwargs: dict[str, Any] = {
                "config_path": settings.config_path or None,
                "checkpointer": self._get_checkpointer(),
                "model_name": settings.model_name,
                "thinking_enabled": settings.thinking_enabled,
                "subagent_enabled": settings.subagent_enabled,
                "plan_mode": settings.plan_mode,
                "max_concurrent_subagents": settings.max_concurrent_subagents,
                "recursion_limit": settings.recursion_limit,
            }
            kwargs.update(overrides)
            self._client_map[key] = DeerFlowClient(**kwargs)

        return self._client_map[key]

    async def get_async_client(self, **overrides) -> Any:
        """Get or create a DeerFlowClient instance backed by an async checkpointer."""
        from deerflow.client import DeerFlowClient

        key = (
            "async",
            settings.checkpointer_type,
            settings.model_name,
            settings.thinking_enabled,
            settings.subagent_enabled,
            settings.plan_mode,
            settings.max_concurrent_subagents,
            settings.recursion_limit,
            frozenset(overrides.items()),
        )

        if key not in self._async_client_map:
            async with self._async_client_lock:
                if key not in self._async_client_map:
                    kwargs: dict[str, Any] = {
                        "config_path": settings.config_path or None,
                        "checkpointer": await self._get_async_checkpointer(),
                        "model_name": settings.model_name,
                        "thinking_enabled": settings.thinking_enabled,
                        "subagent_enabled": settings.subagent_enabled,
                        "plan_mode": settings.plan_mode,
                        "max_concurrent_subagents": settings.max_concurrent_subagents,
                        "recursion_limit": settings.recursion_limit,
                    }
                    kwargs.update(overrides)
                    self._async_client_map[key] = DeerFlowClient(**kwargs)

        return self._async_client_map[key]

    def get_checkpointer(self):
        """Get the shared checkpointer for direct operations."""
        return self._get_checkpointer()

    def _get_checkpointer(self):
        """Create the appropriate checkpointer based on settings."""
        if settings.checkpointer_type == "none":
            return None

        if settings.checkpointer_type == "memory":
            from langgraph.checkpoint.memory import InMemorySaver
            if self._checkpointer is None:
                self._checkpointer = InMemorySaver()
            return self._checkpointer

        if self._checkpointer is not None:
            return self._checkpointer

        from langgraph.checkpoint.sqlite import SqliteSaver

        db_path = Path(settings.checkpointer_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        try:
            saver = SqliteSaver(conn)
            try:
                saver.setup()
            except Exception:
                conn.close()
                raise
            self._sync_sqlite_conn = conn
            self._checkpointer = saver
            atexit.register(conn.close)
            return saver
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            if not settings.allow_memory_fallback:
                raise
            logger.warning(
                "SQLite checkpointer initialization failed; falling back to memory because DEER_FLOW_ALLOW_MEMORY_FALLBACK=true",
                exc_info=True,
            )
            from langgraph.checkpoint.memory import InMemorySaver
            self._checkpointer = InMemorySaver()
            return self._checkpointer

    async def _get_async_checkpointer(self):
        """Create or return the shared async checkpointer for async graph execution."""
        if settings.checkpointer_type == "none":
            return None

        if self._async_checkpointer is not None:
            return self._async_checkpointer

        async with self._async_init_lock:
            # Double-check inside the lock to avoid racing initialization.
            if self._async_checkpointer is not None:
                return self._async_checkpointer

            if settings.checkpointer_type == "memory":
                from langgraph.checkpoint.memory import InMemorySaver

                self._async_checkpointer = InMemorySaver()
                return self._async_checkpointer

            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            db_path = Path(settings.checkpointer_path)
            await asyncio.to_thread(db_path.parent.mkdir, parents=True, exist_ok=True)

            cm = AsyncSqliteSaver.from_conn_string(str(db_path))
            saver = None
            try:
                saver = await cm.__aenter__()
                await saver.setup()
            except Exception:
                # Roll back partial initialization so a retry can succeed and we
                # do not leak an open SQLite connection.
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    logger.warning("async checkpointer cleanup failed during init", exc_info=True)
                raise
            self._async_checkpointer_cm = cm
            self._async_checkpointer = saver
            return self._async_checkpointer

    def _assert_storage_ready(self) -> None:
        """Validate persistent directories/checkpointer at startup."""
        if self._storage_checked:
            return
        self._assert_dir_writable(Path(settings.data_dir))
        if settings.checkpointer_type == "sqlite":
            db_path = Path(settings.checkpointer_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._assert_dir_writable(db_path.parent)
            conn = sqlite3.connect(str(db_path))
            try:
                from langgraph.checkpoint.sqlite import SqliteSaver

                SqliteSaver(conn).setup()
            finally:
                conn.close()
        self._storage_checked = True

    @staticmethod
    def _assert_dir_writable(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".deerflow-write-check"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f"Directory is not writable: {path}") from exc

    def readiness_check(self) -> dict[str, Any]:
        """Return readiness status for /health/ready."""
        checks: dict[str, Any] = {}
        ok = True

        try:
            from deerflow.config.app_config import get_app_config

            config = get_app_config()
            checks["config"] = {"ok": True, "models": len(config.models)}
        except Exception as exc:
            ok = False
            checks["config"] = {"ok": False, "error": str(exc)}

        try:
            self._assert_dir_writable(Path(settings.data_dir))
            checks["data_dir"] = {"ok": True, "path": str(Path(settings.data_dir))}
        except Exception as exc:
            ok = False
            checks["data_dir"] = {"ok": False, "error": str(exc)}

        try:
            if settings.checkpointer_type == "sqlite":
                self._get_checkpointer()
                checks["checkpointer"] = {
                    "ok": True,
                    "type": settings.checkpointer_type,
                    "path": str(Path(settings.checkpointer_path)),
                }
            else:
                checks["checkpointer"] = {"ok": True, "type": settings.checkpointer_type}
        except Exception as exc:
            ok = False
            checks["checkpointer"] = {"ok": False, "error": str(exc)}

        if settings.auth_enabled and not settings.api_keys:
            ok = False
            checks["auth"] = {"ok": False, "error": "auth enabled without API keys"}
        else:
            checks["auth"] = {"ok": True, "enabled": settings.auth_enabled}

        return {"status": "ok" if ok else "error", "checks": checks}

    async def start_client_stream_run(
        self,
        *,
        thread_id: str,
        message: str,
        kwargs: dict[str, Any],
        request_id: str | None = None,
        run_id: str | None = None,
        entrypoint: str = "chat_stream",
        on_disconnect: str = "cancel",
        multitask_strategy: str = "reject",
    ) -> RunRecord:
        """Create a run and stream DeerFlowClient events through the bridge."""
        record = await self.run_manager.create_or_reject(
            thread_id,
            run_id=run_id,
            on_disconnect=DisconnectMode.cancel if on_disconnect == "cancel" else DisconnectMode.continue_,
            multitask_strategy=multitask_strategy,
            metadata={"request_id": request_id, "entrypoint": entrypoint},
            kwargs=kwargs,
        )
        self.mark_thread_running(thread_id)
        task = asyncio.create_task(
            self._produce_client_stream(
                record=record,
                message=message,
                kwargs=kwargs,
                request_id=request_id,
            )
        )
        record.task = task
        return record

    async def _produce_client_stream(
        self,
        *,
        record: RunRecord,
        message: str,
        kwargs: dict[str, Any],
        request_id: str | None,
    ) -> None:
        """Background producer for /chat/stream style runs."""
        run_id = record.run_id
        thread_id = record.thread_id
        try:
            await self.run_manager.set_status(run_id, RunStatus.running)
            await self.stream_bridge.publish(
                run_id,
                "metadata",
                {
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "request_id": request_id,
                },
            )
            # Build a live_event_callback that bypasses LangGraph's per-node
            # custom-stream buffer and publishes events directly to the bridge.
            # This is used by task_tool to push subagent progress events in
            # real time while the tool coroutine is still running (polling).
            bridge = self.stream_bridge

            async def _live_event_callback(event: dict[str, Any]) -> None:
                await bridge.publish(run_id, event.get("type", "custom"), event)

            client = await self.get_async_client(**kwargs)
            _agent_name = client.agent_name
            async with asyncio.timeout(settings.chat_request_timeout):
                async for event in client.astream(message, thread_id=thread_id, live_event_callback=_live_event_callback):
                    if record.abort_event.is_set():
                        break
                    data = event.data
                    if isinstance(data, dict):
                        data = {**data, "_agent_name": _agent_name}
                    await self.stream_bridge.publish(run_id, event.type, data)

            if record.abort_event.is_set():
                await self.run_manager.set_status(run_id, RunStatus.interrupted)
            else:
                await self.run_manager.set_status(run_id, RunStatus.success)
        except asyncio.TimeoutError:
            logger.warning("run timed out after %.0fs (run=%s thread=%s)", settings.chat_request_timeout, run_id, thread_id)
            await self.stream_bridge.publish(run_id, "error", {"error": "Run timed out"})
            await self.run_manager.set_status(run_id, RunStatus.timeout, error="Run timed out")
        except asyncio.CancelledError:
            logger.info("run cancelled (run=%s thread=%s)", run_id, thread_id)
            await self.stream_bridge.publish(run_id, "error", {"error": "Run cancelled"})
            await self.run_manager.set_status(run_id, RunStatus.interrupted)
            raise
        except Exception as exc:
            if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
                logger.debug("Stream producer stopped: event loop closed (run=%s thread=%s)", run_id, thread_id)
            else:
                logger.exception("Unhandled error in run producer (run=%s thread=%s)", run_id, thread_id)
                try:
                    await self.stream_bridge.publish(run_id, "error", {"error": "Internal server error"})
                    await self.run_manager.set_status(run_id, RunStatus.error, error="Internal server error")
                except RuntimeError as bridge_err:
                    if "Event loop is closed" not in str(bridge_err):
                        raise
        finally:
            self.mark_thread_done(thread_id)
            try:
                await self.stream_bridge.publish_end(run_id)
                asyncio.create_task(self.stream_bridge.cleanup(run_id, delay=60))
                asyncio.create_task(self.run_manager.cleanup(run_id, delay=300))
            except RuntimeError as exc:
                if "Event loop is closed" not in str(exc):
                    raise
                logger.debug("Cleanup skipped: event loop closed (run=%s)", run_id)

    async def cancel_run(self, run_id: str, *, action: str = "interrupt") -> bool:
        """Cancel an in-flight run."""
        return await self.run_manager.cancel(run_id, action=action)

    def mark_thread_running(self, thread_id: str):
        with self._thread_lock:
            self._running_threads.add(thread_id)

    def mark_thread_done(self, thread_id: str):
        with self._thread_lock:
            self._running_threads.discard(thread_id)

    def is_thread_running(self, thread_id: str) -> bool:
        with self._thread_lock:
            return thread_id in self._running_threads

    def delete_thread_completely(self, thread_id: str) -> dict[str, Any]:
        """Delete both checkpointer data and file system data for a thread.

        Atomically refuses to delete a thread that is currently running.
        """
        import shutil

        # Atomic check-and-reject: hold the running-threads lock so a concurrent
        # mark_thread_running cannot start a run between our check and delete.
        with self._thread_lock:
            if thread_id in self._running_threads:
                return {"success": False, "running": True, "detail": f"Thread {thread_id} is currently running"}

            # 1. Delete from checkpointer
            checkpointer = self.get_checkpointer()
            if checkpointer is not None and hasattr(checkpointer, "delete_thread"):
                try:
                    checkpointer.delete_thread(thread_id)
                except Exception:
                    logger.warning("checkpointer delete_thread failed for %s", thread_id, exc_info=True)

            # 2. Delete file system data
            try:
                from deerflow.config.paths import get_paths
                paths = get_paths()
                thread_dir = paths.thread_dir(thread_id)
                if thread_dir.exists():
                    try:
                        shutil.rmtree(thread_dir)
                    except PermissionError:
                        # The sandbox container starts as root (the image's init
                        # needs root to boot), so it leaves root-owned files in the
                        # bind-mounted thread directory — the daemon-created workdir
                        # and image-init scaffolding (e.g. .openhands/skills). The
                        # backend process cannot delete those, so fall back to a
                        # throwaway root container to remove the whole directory.
                        _rmtree_via_root_container(paths, thread_id)
            except Exception:
                logger.warning("filesystem cleanup failed for %s", thread_id, exc_info=True)

            self._running_threads.discard(thread_id)

        return {"success": True, "message": f"Deleted thread {thread_id}"}

    def update_thread_metadata(self, thread_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        """Update thread metadata via direct SQLite write.

        Updates the metadata JSON on the latest checkpoint for the thread.
        """
        import json
        if settings.checkpointer_type != "sqlite":
            return {"success": False, "detail": "Only SQLite supports metadata update"}

        db_path = Path(settings.checkpointer_path)
        if not db_path.exists():
            return {"success": False, "detail": "Checkpointer DB not found"}

        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            # Find the latest checkpoint by selecting the one with highest step in metadata
            cursor.execute("""
                SELECT thread_id, checkpoint_ns, checkpoint_id, metadata
                FROM checkpoints
                WHERE thread_id = ?
                ORDER BY json_extract(metadata, '$.step') DESC
                LIMIT 1
            """, (thread_id,))
            row = cursor.fetchone()
            if not row:
                return {"success": False, "detail": f"No checkpoints found for {thread_id}"}

            # Merge new metadata into existing. The DB row may be corrupted
            # (truncated write, manual edit, schema mismatch); surface a clear
            # error rather than the raw JSONDecodeError trace.
            try:
                existing = json.loads(row["metadata"]) if row["metadata"] else {}
            except json.JSONDecodeError:
                logger.exception(
                    "Corrupted metadata JSON for thread %s; resetting", thread_id
                )
                existing = {}
            existing.update(metadata)
            merged = json.dumps(existing)

            cursor.execute("""
                UPDATE checkpoints
                SET metadata = ?
                WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
            """, [merged, thread_id, row["checkpoint_ns"], row["checkpoint_id"]])
            conn.commit()
            return {"success": True, "message": f"Updated metadata for {thread_id}"}
        except Exception:
            # Log full exception server-side; return generic message to caller.
            logger.exception("update_thread_metadata failed for %s", thread_id)
            return {"success": False, "detail": "Failed to update thread metadata"}
        finally:
            conn.close()

    async def shutdown(self):
        """Cleanup on shutdown."""
        if self.scheduler_service is not None:
            try:
                await self.scheduler_service.stop()
            except Exception:
                logger.warning("Error stopping scheduler service", exc_info=True)
            finally:
                self.scheduler_service = None

        try:
            from deerflow.agents.memory.queue import get_memory_queue
            from deerflow.config.memory_config import get_memory_config

            if get_memory_config().enabled:
                await asyncio.to_thread(get_memory_queue().flush)
        except Exception:
            pass

        self._client_map.clear()
        self._async_client_map.clear()
        try:
            from deerflow.sandbox.sandbox_provider import shutdown_sandbox_provider

            shutdown_sandbox_provider()
        except Exception:
            logger.warning("Error during sandbox provider cleanup", exc_info=True)
        if self._async_checkpointer_cm is not None:
            try:
                await self._async_checkpointer_cm.__aexit__(None, None, None)
            except Exception:
                logger.warning("Error during async checkpointer cleanup", exc_info=True)
            finally:
                self._async_checkpointer_cm = None
                self._async_checkpointer = None
        if self._sync_sqlite_conn is not None:
            try:
                self._sync_sqlite_conn.close()
            except Exception:
                logger.warning("Error closing sync SQLite connection", exc_info=True)
            finally:
                self._sync_sqlite_conn = None
        self._checkpointer = None
        self._running_threads.clear()


def get_client_manager() -> ClientManager:
    global _client_manager
    if _client_manager is None:
        with _lock:
            if _client_manager is None:
                _client_manager = ClientManager()
    return _client_manager
