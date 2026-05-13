from __future__ import annotations

import fnmatch
import os
import re
import shlex
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import PurePosixPath

from deerflow.config.paths import get_paths
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import SandboxProvider
from deerflow.sandbox.search import GrepMatch

DEFAULT_IMAGE = "enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest"
DEFAULT_REPLICAS = 3
DEFAULT_CONTAINER_PREFIX = "deer-flow-sandbox"
DEFAULT_IDLE_TIMEOUT_SECONDS = 600
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600

_SAFE_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class _SandboxRecord:
    sandbox: AioSandbox
    thread_id: str
    last_used: float


class AioSandbox(Sandbox):
    """Docker-backed sandbox that exposes mounted paths directly in-container."""

    def __init__(self, id: str, container_name: str, *, timeout: int = DEFAULT_COMMAND_TIMEOUT_SECONDS) -> None:
        super().__init__(id)
        self.container_name = container_name
        self.timeout = timeout

    def _docker_exec(
        self,
        args: list[str],
        *,
        input_data: str | bytes | None = None,
        text: bool = True,
        check: bool = False,
    ) -> subprocess.CompletedProcess:
        cmd = ["docker", "exec", "-i", self.container_name, *args]
        return subprocess.run(
            cmd,
            input=input_data,
            shell=False,
            capture_output=True,
            text=text,
            timeout=self.timeout,
            check=check,
        )

    @staticmethod
    def _output_from_result(result: subprocess.CompletedProcess) -> str:
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")

        output = stdout
        if stderr:
            output += f"\nStd Error:\n{stderr}" if output else stderr
        if result.returncode != 0:
            output += f"\nExit Code: {result.returncode}"
        return output if output else "(no output)"

    @staticmethod
    def _quote(path: str) -> str:
        return shlex.quote(path)

    def execute_command(self, command: str) -> str:
        result = self._docker_exec(["/bin/bash", "-lc", command])
        if result.returncode == 126 or result.returncode == 127:
            result = self._docker_exec(["/bin/sh", "-lc", command])
        return self._output_from_result(result)

    def read_file(self, path: str) -> str:
        result = self._docker_exec(["cat", path])
        if result.returncode != 0:
            raise FileNotFoundError(path)
        return result.stdout

    def list_dir(self, path: str, max_depth=2) -> list[str]:
        script = r"""
import os, sys, fnmatch
root = os.path.realpath(sys.argv[1])
max_depth = int(sys.argv[2])
ignore = sys.argv[3].split("\0") if sys.argv[3] else []
if not os.path.isdir(root):
    sys.exit(0)

def ignored(name):
    return any(fnmatch.fnmatch(name, pat) for pat in ignore)

result = []

def within(candidate):
    try:
        return os.path.commonpath([root, os.path.realpath(candidate)]) == root
    except OSError:
        return False

def walk(current, depth):
    if depth > max_depth:
        return
    try:
        entries = sorted(os.listdir(current))
    except OSError:
        return
    for name in entries:
        if ignored(name):
            continue
        item = os.path.join(current, name)
        try:
            real = os.path.realpath(item)
            if not within(real):
                continue
            is_dir = os.path.isdir(real)
        except OSError:
            continue
        result.append(real + ("/" if is_dir else ""))
        if is_dir and depth < max_depth:
            walk(real, depth + 1)

walk(root, 1)
print("\n".join(result))
"""
        from deerflow.sandbox.search import IGNORE_PATTERNS

        result = self._docker_exec(["python3", "-", path, str(max_depth), "\0".join(IGNORE_PATTERNS)], input_data=script)
        if result.returncode != 0:
            result = self._docker_exec(["python", "-", path, str(max_depth), "\0".join(IGNORE_PATTERNS)], input_data=script)
        if result.returncode != 0 or not result.stdout:
            return []
        return [line for line in result.stdout.splitlines() if line]

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        quoted_path = self._quote(path)
        mode = "ab" if append else "wb"
        script = (
            "import os, sys\n"
            f"path = {path!r}\n"
            "parent = os.path.dirname(path)\n"
            "if parent:\n"
            "    os.makedirs(parent, exist_ok=True)\n"
            f"with open(path, {mode!r}) as f:\n"
            "    f.write(sys.stdin.buffer.read())\n"
        )
        result = self._docker_exec(["python3", "-c", script], input_data=content.encode("utf-8"), text=False)
        if result.returncode != 0:
            result = self._docker_exec(
                ["/bin/sh", "-c", f"mkdir -p $(dirname {quoted_path}) && cat {'>>' if append else '>'} {quoted_path}"],
                input_data=content.encode("utf-8"),
                text=False,
            )
        if result.returncode != 0:
            raise OSError(f"Failed to write file {path}: {self._output_from_result(result)}")

    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
        script = r"""
import fnmatch, os, sys
from pathlib import PurePosixPath
root = os.path.realpath(sys.argv[1])
pattern = sys.argv[2]
include_dirs = sys.argv[3] == "1"
max_results = int(sys.argv[4])
ignore = sys.argv[5].split("\0") if sys.argv[5] else []
if not os.path.exists(root):
    raise FileNotFoundError(root)
if not os.path.isdir(root):
    raise NotADirectoryError(root)

def ignored(name):
    return any(fnmatch.fnmatch(name, pat) for pat in ignore)

def matches(rel):
    p = PurePosixPath(rel)
    return p.match(pattern) or (pattern.startswith("**/") and p.match(pattern[3:]))

out = []
truncated = False
for current, dirs, files in os.walk(root):
    dirs[:] = [d for d in dirs if not ignored(d)]
    rel_dir = os.path.relpath(current, root)
    if rel_dir == ".":
        rel_dir = ""
    if include_dirs:
        for name in dirs:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if matches(rel):
                out.append(os.path.join(current, name))
                if len(out) >= max_results:
                    truncated = True
                    break
    if truncated:
        break
    for name in files:
        if ignored(name):
            continue
        rel = f"{rel_dir}/{name}" if rel_dir else name
        if matches(rel):
            out.append(os.path.join(current, name))
            if len(out) >= max_results:
                truncated = True
                break
    if truncated:
        break
print("1" if truncated else "0")
print("\n".join(out))
"""
        from deerflow.sandbox.search import IGNORE_PATTERNS

        result = self._docker_exec(
            ["python3", "-", path, pattern, "1" if include_dirs else "0", str(max_results), "\0".join(IGNORE_PATTERNS)],
            input_data=script,
        )
        if result.returncode != 0:
            raise FileNotFoundError(path)
        lines = result.stdout.splitlines()
        truncated = bool(lines and lines[0] == "1")
        return lines[1:], truncated

    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepMatch], bool]:
        script = r"""
import fnmatch, json, os, re, sys
from pathlib import PurePosixPath
root = os.path.realpath(sys.argv[1])
source = sys.argv[2]
glob_pattern = sys.argv[3] or None
literal = sys.argv[4] == "1"
case_sensitive = sys.argv[5] == "1"
max_results = int(sys.argv[6])
ignore = sys.argv[7].split("\0") if sys.argv[7] else []
if not os.path.exists(root):
    raise FileNotFoundError(root)
if not os.path.isdir(root):
    raise NotADirectoryError(root)

def ignored(name):
    return any(fnmatch.fnmatch(name, pat) for pat in ignore)

def path_matches(pat, rel):
    p = PurePosixPath(rel)
    return p.match(pat) or (pat.startswith("**/") and p.match(pat[3:]))

flags = 0 if case_sensitive else re.IGNORECASE
regex = re.compile(re.escape(source) if literal else source, flags)
matches = []
truncated = False
for current, dirs, files in os.walk(root):
    dirs[:] = [d for d in dirs if not ignored(d)]
    rel_dir = os.path.relpath(current, root)
    if rel_dir == ".":
        rel_dir = ""
    for name in files:
        if ignored(name):
            continue
        full = os.path.join(current, name)
        rel = f"{rel_dir}/{name}" if rel_dir else name
        if glob_pattern and not path_matches(glob_pattern, rel):
            continue
        try:
            if os.path.islink(full) or os.path.getsize(full) > 1000000:
                continue
            with open(full, "rb") as sample:
                if b"\0" in sample.read(8192):
                    continue
            with open(full, encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, 1):
                    if len(line) > 2000:
                        continue
                    if regex.search(line):
                        line = line.rstrip("\r\n")
                        if len(line) > 200:
                            line = line[:197] + "..."
                        matches.append({"path": os.path.realpath(full), "line_number": line_number, "line": line})
                        if len(matches) >= max_results:
                            truncated = True
                            raise StopIteration
        except StopIteration:
            break
        except OSError:
            continue
    if truncated:
        break
print(json.dumps({"truncated": truncated, "matches": matches}))
"""
        from deerflow.sandbox.search import IGNORE_PATTERNS

        result = self._docker_exec(
            [
                "python3",
                "-",
                path,
                pattern,
                glob or "",
                "1" if literal else "0",
                "1" if case_sensitive else "0",
                str(max_results),
                "\0".join(IGNORE_PATTERNS),
            ],
            input_data=script,
        )
        if result.returncode != 0:
            raise FileNotFoundError(path)

        import json

        payload = json.loads(result.stdout or '{"truncated": false, "matches": []}')
        return [GrepMatch(**item) for item in payload["matches"]], bool(payload["truncated"])

    def update_file(self, path: str, content: bytes) -> None:
        quoted_path = self._quote(path)
        result = self._docker_exec(
            ["/bin/sh", "-c", f"mkdir -p $(dirname {quoted_path}) && cat > {quoted_path}"],
            input_data=content,
            text=False,
        )
        if result.returncode != 0:
            raise OSError(f"Failed to update file {path}: {self._output_from_result(result)}")


class AioSandboxProvider(SandboxProvider):
    """Docker CLI based sandbox provider.

    Each thread gets one long-running container. Thread data is bind-mounted at
    /mnt/user-data, skills are mounted read-only, and configured custom mounts
    are passed through with their declared read-only mode.
    """

    uses_thread_data_mounts = False

    def __init__(self) -> None:
        from deerflow.config import get_app_config

        config = get_app_config()
        sandbox_cfg = config.sandbox
        self.image = sandbox_cfg.image or DEFAULT_IMAGE
        self.replicas = sandbox_cfg.replicas or DEFAULT_REPLICAS
        self.container_prefix = sandbox_cfg.container_prefix or DEFAULT_CONTAINER_PREFIX
        self.idle_timeout = DEFAULT_IDLE_TIMEOUT_SECONDS if sandbox_cfg.idle_timeout is None else sandbox_cfg.idle_timeout
        self.environment = sandbox_cfg.environment or {}
        self.mounts = sandbox_cfg.mounts or []
        self.security_opt = getattr(sandbox_cfg, "security_opt", None) or []
        self.skills_path = config.skills.get_skills_path()
        self.skills_container_path = config.skills.container_path
        self._lock = threading.Lock()
        self._records: OrderedDict[str, _SandboxRecord] = OrderedDict()
        self._verify_docker_available()

    def _verify_docker_available(self) -> None:
        try:
            result = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], shell=False, capture_output=True, text=True, timeout=15)
        except FileNotFoundError as exc:
            raise RuntimeError("Docker CLI was not found on PATH. Install Docker before using AioSandboxProvider.") from exc
        if result.returncode != 0:
            raise RuntimeError(f"Docker daemon is not available: {(result.stderr or result.stdout).strip()}")

    @staticmethod
    def _safe_thread_id(thread_id: str | None) -> str:
        value = thread_id or "default"
        if not _SAFE_THREAD_ID_RE.fullmatch(value):
            raise ValueError(f"Invalid thread_id {value!r}: only alphanumeric characters, hyphens, and underscores are allowed.")
        return value

    def _sandbox_id(self, thread_id: str) -> str:
        return f"aio-{thread_id}"

    def _container_name(self, sandbox_id: str) -> str:
        return f"{self.container_prefix}-{sandbox_id}"

    def _run_docker(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", *args], shell=False, capture_output=True, text=True, timeout=60, check=check)

    def _build_run_args(self, thread_id: str, sandbox_id: str) -> list[str]:
        paths = get_paths()
        paths.ensure_thread_dirs(thread_id)
        args = [
            "run",
            "-d",
            "--name",
            self._container_name(sandbox_id),
            "--label",
            "deerflow.sandbox.provider=aio",
            "--label",
            f"deerflow.sandbox.id={sandbox_id}",
            "--workdir",
            "/mnt/user-data/workspace",
            "-v",
            f"{paths.host_sandbox_user_data_dir(thread_id)}:/mnt/user-data:rw",
            "-v",
            f"{paths.host_acp_workspace_dir(thread_id)}:/mnt/acp-workspace:rw",
        ]
        if self.skills_path.exists():
            args.extend(["-v", f"{self.skills_path}:{self.skills_container_path}:ro"])
        for mount in self.mounts:
            if not os.path.exists(mount.host_path):
                continue
            mode = "ro" if mount.read_only else "rw"
            args.extend(["-v", f"{mount.host_path}:{mount.container_path}:{mode}"])
        for opt in self.security_opt:
            if opt:
                args.extend(["--security-opt", opt])
        for key, value in self.environment.items():
            resolved = os.environ.get(value[1:], "") if isinstance(value, str) and value.startswith("$") else value
            args.extend(["-e", f"{key}={resolved}"])
        args.extend([self.image, "sh", "-c", "trap 'exit 0' TERM INT; while :; do sleep 3600; done"])
        return args

    def _start_container(self, thread_id: str, sandbox_id: str) -> AioSandbox:
        name = self._container_name(sandbox_id)
        self._run_docker(["rm", "-f", name], check=False)
        try:
            self._run_docker(_as_str_list(self._build_run_args(thread_id, sandbox_id)))
        except subprocess.CalledProcessError as exc:
            diagnostic = (exc.stderr or exc.stdout or "").strip()
            raise RuntimeError(f"Failed to start sandbox container {name}: {diagnostic}") from exc
        return AioSandbox(sandbox_id, name)

    def _evict_if_needed(self) -> None:
        while len(self._records) >= self.replicas and self._records:
            sandbox_id, record = self._records.popitem(last=False)
            self._remove_container(record.sandbox.container_name)

    def _remove_container(self, container_name: str) -> None:
        self._run_docker(["rm", "-f", container_name], check=False)

    def _cleanup_idle_locked(self) -> None:
        if self.idle_timeout == 0:
            return
        now = time.time()
        expired = [
            sandbox_id
            for sandbox_id, record in self._records.items()
            if now - record.last_used > self.idle_timeout
        ]
        for sandbox_id in expired:
            record = self._records.pop(sandbox_id)
            self._remove_container(record.sandbox.container_name)

    def acquire(self, thread_id: str | None = None) -> str:
        safe_thread_id = self._safe_thread_id(thread_id)
        sandbox_id = self._sandbox_id(safe_thread_id)
        with self._lock:
            self._cleanup_idle_locked()
            record = self._records.get(sandbox_id)
            if record is not None:
                record.last_used = time.time()
                self._records.move_to_end(sandbox_id)
                return sandbox_id
            self._evict_if_needed()
            sandbox = self._start_container(safe_thread_id, sandbox_id)
            self._records[sandbox_id] = _SandboxRecord(sandbox=sandbox, thread_id=safe_thread_id, last_used=time.time())
            return sandbox_id

    def get(self, sandbox_id: str) -> Sandbox | None:
        with self._lock:
            record = self._records.get(sandbox_id)
            if record is None:
                return None
            record.last_used = time.time()
            self._records.move_to_end(sandbox_id)
            return record.sandbox

    def release(self, sandbox_id: str) -> None:
        with self._lock:
            record = self._records.get(sandbox_id)
            if record is not None:
                record.last_used = time.time()
                self._records.move_to_end(sandbox_id)

    def shutdown(self) -> None:
        with self._lock:
            records = list(self._records.values())
            self._records.clear()
        for record in records:
            self._remove_container(record.sandbox.container_name)


def _as_str_list(values: list[object]) -> list[str]:
    return [str(value) for value in values]
