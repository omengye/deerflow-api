"""Local endpoint discovery and single-instance locking for the ACP daemon."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO

RUNTIME_DIR_ENV = "DEER_FLOW_ACP_RUNTIME_DIR"
ENDPOINT_FILENAME = "endpoint.json"
LOCK_FILENAME = "daemon.lock"


def _portable_root() -> Path | None:
    """Return the ZIP product root when running from bundled Python."""

    executable = Path(sys.executable).resolve()
    candidates = [executable.parent]
    if executable.parent.name.lower() in {"runtime", "bin"}:
        candidates.append(executable.parent.parent)
    for root in candidates:
        if (
            (root / "resources" / "default-config.yaml").is_file()
            or (root / "user-data" / "config" / "config.yaml").is_file()
        ):
            return root
    return None


def get_runtime_dir(override: str | Path | None = None) -> Path:
    """Return the per-user directory used to discover the local ACP daemon."""

    if override is not None:
        path = Path(override).expanduser()
    elif configured := os.getenv(RUNTIME_DIR_ENV):
        path = Path(configured).expanduser()
    elif portable_root := _portable_root():
        path = portable_root / "user-data" / "runtime" / "acp"
    elif local_app_data := os.getenv("LOCALAPPDATA"):
        path = Path(local_app_data) / "DeerFlow" / "acp"
    elif xdg_runtime := os.getenv("XDG_RUNTIME_DIR"):
        path = Path(xdg_runtime) / "deerflow-acp"
    elif xdg_cache := os.getenv("XDG_CACHE_HOME"):
        path = Path(xdg_cache) / "deerflow" / "acp"
    else:
        path = Path.home() / ".cache" / "deerflow" / "acp"
    return path.resolve()


def ensure_runtime_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        # Windows ACLs, rather than POSIX mode bits, protect LOCALAPPDATA.
        pass
    return path


@dataclass(frozen=True, slots=True)
class DaemonEndpoint:
    host: str
    port: int
    token: str
    pid: int
    build_id: str
    config_path: str

    @classmethod
    def load(cls, path: Path) -> "DaemonEndpoint":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            host=str(payload["host"]),
            port=int(payload["port"]),
            token=str(payload["token"]),
            pid=int(payload["pid"]),
            build_id=str(payload["build_id"]),
            config_path=str(payload["config_path"]),
        )

    def publish(self, path: Path) -> None:
        ensure_runtime_dir(path.parent)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(asdict(self), ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class DaemonAlreadyRunning(RuntimeError):
    """Raised when another process holds the per-user ACP daemon lock."""


class SingleInstanceLock:
    """Cross-platform advisory lock held for the complete daemon lifetime."""

    def __init__(self, path: Path):
        self.path = path
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        ensure_runtime_dir(self.path.parent)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB  # type: ignore[attr-defined]
                )
        except OSError as exc:
            handle.close()
            raise DaemonAlreadyRunning(
                f"Another DeerFlow ACP daemon is already running ({self.path})"
            ) from exc
        handle.seek(0)
        handle.write(f"{os.getpid()}\n".encode("ascii"))
        handle.truncate()
        handle.flush()
        self._file = handle

    def release(self) -> None:
        handle = self._file
        self._file = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    handle.fileno(), fcntl.LOCK_UN  # type: ignore[attr-defined]
                )
        finally:
            handle.close()

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
