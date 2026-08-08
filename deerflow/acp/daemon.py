"""Long-lived local ACP daemon used by the native stdio bridge."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import importlib.metadata
import json
import logging
import os
import secrets
import signal
import sys
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import acp

from .agent import DeerFlowACPAgent
from .config import LocalACPConfig
from .daemon_endpoint import (
    ENDPOINT_FILENAME,
    LOCK_FILENAME,
    DaemonAlreadyRunning,
    DaemonEndpoint,
    SingleInstanceLock,
    ensure_runtime_dir,
    get_runtime_dir,
)
from .runtime import LocalACPRuntime
from .session_store import LocalACPSessionStore

logger = logging.getLogger(__name__)
_HANDSHAKE_VERSION = "DFACP/1"
_HANDSHAKE_LIMIT = 4096
_HANDSHAKE_TIMEOUT_SECONDS = 3.0


def _build_id() -> str:
    try:
        return importlib.metadata.version("deerflow-api")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


def _env_enabled(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError, RuntimeError):
        pass


class ACPDaemon:
    """Serve one ACP client at a time while reusing the expensive runtime."""

    def __init__(
        self,
        config: LocalACPConfig,
        store: LocalACPSessionStore,
        runtime: LocalACPRuntime,
        runtime_dir: Path,
        *,
        agent_factory: Callable[..., DeerFlowACPAgent] = DeerFlowACPAgent,
        token: str | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.runtime = runtime
        self.runtime_dir = ensure_runtime_dir(runtime_dir)
        self.endpoint_path = self.runtime_dir / ENDPOINT_FILENAME
        self.token = token or secrets.token_urlsafe(32)
        self.agent_factory = agent_factory
        self.endpoint: DaemonEndpoint | None = None
        self._server: asyncio.Server | None = None
        self._state_lock = asyncio.Lock()
        self._active = False
        self._active_task: asyncio.Task[Any] | None = None
        self._stop_requested = asyncio.Event()
        self._closed = False

    @property
    def stop_requested(self) -> asyncio.Event:
        return self._stop_requested

    def request_stop(self) -> None:
        self._stop_requested.set()

    async def start(self) -> DaemonEndpoint:
        if self._server is not None:
            assert self.endpoint is not None
            return self.endpoint
        self._server = await asyncio.start_server(self._handle_connection, "127.0.0.1", 0)
        socket = self._server.sockets[0]
        host, port = socket.getsockname()[:2]
        endpoint = DaemonEndpoint(
            host=str(host),
            port=int(port),
            token=self.token,
            pid=os.getpid(),
            build_id=_build_id(),
            config_path=str(self.config.config_path.resolve()),
        )
        endpoint.publish(self.endpoint_path)
        self.endpoint = endpoint
        logger.info("ACP daemon ready on %s:%d", endpoint.host, endpoint.port)
        return endpoint

    async def wait(self) -> None:
        await self._stop_requested.wait()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        task = self._active_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            current = DaemonEndpoint.load(self.endpoint_path)
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            current = None
        if current is not None and current.pid == os.getpid() and current.token == self.token:
            self.endpoint_path.unlink(missing_ok=True)
        logger.info("ACP daemon stopped")

    async def _read_handshake(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str] | None:
        try:
            raw = await asyncio.wait_for(
                reader.readline(), timeout=_HANDSHAKE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            return None
        if not raw or len(raw) > _HANDSHAKE_LIMIT:
            return None
        try:
            version, token, command = raw.decode("utf-8").strip().split(" ", 2)
        except (UnicodeDecodeError, ValueError):
            return None
        if version != _HANDSHAKE_VERSION or not hmac.compare_digest(token, self.token):
            return None
        return command.upper(), token

    async def _reply(self, writer: asyncio.StreamWriter, value: str) -> None:
        writer.write(value.encode("utf-8") + b"\n")
        await writer.drain()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        handshake = await self._read_handshake(reader)
        if handshake is None:
            await self._reply(writer, "UNAUTHORIZED")
            await _close_writer(writer)
            return
        command, _token = handshake
        if command == "STATUS":
            await self._reply(writer, f"OK {os.getpid()} {_build_id()}")
            await _close_writer(writer)
            return
        if command == "STOP":
            await self._reply(writer, "OK")
            self.request_stop()
            await _close_writer(writer)
            return
        if command != "ACP":
            await self._reply(writer, "ERROR unsupported-command")
            await _close_writer(writer)
            return

        async with self._state_lock:
            if self._active:
                await self._reply(writer, "BUSY")
                await _close_writer(writer)
                return
            self._active = True
            self._active_task = asyncio.current_task()

        await self._reply(writer, "OK")
        agent = self.agent_factory(self.config, self.store, self.runtime)
        try:
            await acp.run_agent(
                agent,
                input_stream=writer,
                output_stream=reader,
                use_unstable_protocol=False,
            )
        except (ConnectionError, asyncio.IncompleteReadError):
            logger.debug("ACP bridge disconnected")
        finally:
            await agent.shutdown()
            async with self._state_lock:
                self._active = False
                self._active_task = None
            await _close_writer(writer)


async def _warm_wsl_sandbox() -> None:
    from deerflow.config import get_app_config
    from deerflow.sandbox.provider_paths import (
        WSL_SANDBOX_PROVIDER_PATH,
        normalize_sandbox_provider_path,
    )

    app_config = get_app_config()
    if normalize_sandbox_provider_path(app_config.sandbox.use) != WSL_SANDBOX_PROVIDER_PATH:
        return
    from deerflow.sandbox.sandbox_provider import get_sandbox_provider

    provider = get_sandbox_provider()
    sandbox_id = provider.acquire()
    sandbox = provider.get(sandbox_id)
    if sandbox is None:
        raise RuntimeError("WSL sandbox warmup did not return a sandbox")
    result = await asyncio.to_thread(sandbox.execute_command, "true")
    if "Exit Code:" in result:
        raise RuntimeError(f"WSL sandbox warmup failed: {result}")


def _install_signal_handlers(daemon: ACPDaemon) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, daemon.request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows console Ctrl+C is still handled by KeyboardInterrupt in main().
            pass


async def _run_daemon(
    config_path: str | None,
    runtime_dir: Path,
    *,
    warmup: bool,
    warmup_sandbox: bool,
) -> None:
    config = LocalACPConfig.from_file(config_path)
    config.prepare_environment()
    store = LocalACPSessionStore(config.session_store_path)
    store.setup()
    runtime = LocalACPRuntime(config)
    daemon = ACPDaemon(config, store, runtime, runtime_dir)
    try:
        await runtime.open()
        if warmup:
            logger.info("Warming DeerFlow agent graph")
            await runtime.warmup()
        if warmup_sandbox:
            logger.info("Warming configured WSL sandbox")
            await _warm_wsl_sandbox()
        await daemon.start()
        _install_signal_handlers(daemon)
        await daemon.wait()
    finally:
        await daemon.close()
        await runtime.close()
        store.close()
        try:
            from deerflow.sandbox.sandbox_provider import shutdown_sandbox_provider

            shutdown_sandbox_provider()
        except Exception:
            logger.exception("Failed to shut down sandbox provider")


def _configure_logging(runtime_dir: Path, level: str) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(numeric_level)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        runtime_dir / "daemon.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.handlers.clear()
    root.addHandler(stderr_handler)
    root.addHandler(file_handler)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the long-lived local DeerFlow ACP daemon")
    parser.add_argument("--config", help="Path to DeerFlow config.yaml")
    parser.add_argument("--runtime-dir", help="Override the per-user daemon discovery directory")
    parser.add_argument("--no-warmup", action="store_true", help="Skip agent graph warmup")
    parser.add_argument(
        "--no-sandbox-warmup",
        action="store_true",
        help="Skip the harmless WSL startup probe",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    args = _parser().parse_args()
    runtime_dir = ensure_runtime_dir(get_runtime_dir(args.runtime_dir))
    _configure_logging(runtime_dir, args.log_level)
    lock = SingleInstanceLock(runtime_dir / LOCK_FILENAME)
    try:
        with lock:
            # Holding the lock proves that a pre-existing endpoint is stale.
            (runtime_dir / ENDPOINT_FILENAME).unlink(missing_ok=True)
            asyncio.run(
                _run_daemon(
                    args.config,
                    runtime_dir,
                    warmup=(
                        not args.no_warmup
                        and _env_enabled("DEER_FLOW_ACP_DAEMON_WARMUP")
                    ),
                    warmup_sandbox=(
                        not args.no_sandbox_warmup
                        and _env_enabled("DEER_FLOW_ACP_DAEMON_SANDBOX_WARMUP")
                    ),
                )
            )
    except DaemonAlreadyRunning as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
