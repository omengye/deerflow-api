import logging
import threading
from collections import OrderedDict
from collections.abc import Collection
from pathlib import Path

from deerflow.sandbox.local.local_sandbox import LocalSandbox, PathMapping
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import SandboxProvider

logger = logging.getLogger(__name__)

_singleton: LocalSandbox | None = None
_USER_DATA_VIRTUAL_PREFIX = "/mnt/user-data"
_ACP_WORKSPACE_VIRTUAL_PREFIX = "/mnt/acp-workspace"
DEFAULT_MAX_CACHED_THREAD_SANDBOXES = 256


def build_host_fs_path_mappings(*, skills_path: Path | None = None) -> list[PathMapping]:
    """
    Build path mappings for host-filesystem-backed sandboxes.

    Maps container paths to actual local paths, including the skills directory
    and any custom mounts configured in ``config.yaml``. Shared by
    ``LocalSandboxProvider`` and ``LocalWslProvider`` because both store
    files on the host filesystem and expose them through virtual container
    paths to the agent.

    Returns:
        List of path mappings.
    """
    mappings: list[PathMapping] = []

    try:
        from deerflow.config import get_app_config

        config = get_app_config()
        if skills_path is None:
            from deerflow.skills.projection import get_skill_projection

            skills_path = get_skill_projection().path
        container_path = config.skills.container_path

        # Only add mapping if skills directory exists
        if skills_path.exists():
            mappings.append(
                PathMapping(
                    container_path=container_path,
                    local_path=str(skills_path),
                    read_only=True,  # Skills directory is always read-only
                )
            )

        # Map custom mounts from sandbox config
        _RESERVED_CONTAINER_PREFIXES = [container_path, _ACP_WORKSPACE_VIRTUAL_PREFIX, _USER_DATA_VIRTUAL_PREFIX]
        sandbox_config = config.sandbox
        if sandbox_config and sandbox_config.mounts:
            for mount in sandbox_config.mounts:
                host_path = Path(mount.host_path)
                container_path = mount.container_path.rstrip("/") or "/"

                if not host_path.is_absolute():
                    logger.warning(
                        "Mount host_path must be absolute, skipping: %s -> %s",
                        mount.host_path,
                        mount.container_path,
                    )
                    continue

                if not container_path.startswith("/"):
                    logger.warning(
                        "Mount container_path must be absolute, skipping: %s -> %s",
                        mount.host_path,
                        mount.container_path,
                    )
                    continue

                # Reject mounts that conflict with reserved container paths
                if any(container_path == p or container_path.startswith(p + "/") for p in _RESERVED_CONTAINER_PREFIXES):
                    logger.warning(
                        "Mount container_path conflicts with reserved prefix, skipping: %s",
                        mount.container_path,
                    )
                    continue
                # Ensure the host path exists before adding mapping
                if host_path.exists():
                    mappings.append(
                        PathMapping(
                            container_path=container_path,
                            local_path=str(host_path.resolve()),
                            read_only=mount.read_only,
                        )
                    )
                else:
                    logger.warning(
                        "Mount host_path does not exist, skipping: %s -> %s",
                        mount.host_path,
                        mount.container_path,
                    )
    except Exception as e:
        # Log but don't fail if config loading fails
        logger.warning("Could not setup path mappings: %s", e, exc_info=True)

    return mappings


class LocalSandboxProvider(SandboxProvider):
    """Local-filesystem sandbox provider with per-thread path scoping."""

    uses_thread_data_mounts = True

    def __init__(self, max_cached_threads: int = DEFAULT_MAX_CACHED_THREAD_SANDBOXES):
        """Initialize the local sandbox provider with path mappings."""
        self._path_mappings = self._build_non_skill_path_mappings()
        self._generic_sandbox: LocalSandbox | None = None
        self._thread_sandboxes: OrderedDict[str, LocalSandbox] = OrderedDict()
        self._max_cached_threads = max_cached_threads
        self._lock = threading.Lock()

    @staticmethod
    def _build_non_skill_path_mappings() -> list[PathMapping]:
        """Build custom mounts without ever exposing the source Skill tree."""
        from deerflow.config import get_app_config

        config = get_app_config()
        skills_container = config.skills.container_path.rstrip("/") or "/"
        mappings = build_host_fs_path_mappings(skills_path=Path("__missing_skills_projection__"))
        return [
            mapping
            for mapping in mappings
            if (mapping.container_path.rstrip("/") or "/") != skills_container
        ]

    @staticmethod
    def _build_thread_path_mappings(thread_id: str) -> list[PathMapping]:
        """Build per-thread mappings for /mnt/user-data and /mnt/acp-workspace."""
        from deerflow.config.paths import get_paths

        paths = get_paths()
        paths.ensure_thread_dirs(thread_id)

        return [
            PathMapping(
                container_path=_USER_DATA_VIRTUAL_PREFIX,
                local_path=str(paths.sandbox_user_data_dir(thread_id)),
                read_only=False,
            ),
            PathMapping(
                container_path=f"{_USER_DATA_VIRTUAL_PREFIX}/workspace",
                local_path=str(paths.sandbox_work_dir(thread_id)),
                read_only=False,
            ),
            PathMapping(
                container_path=f"{_USER_DATA_VIRTUAL_PREFIX}/uploads",
                local_path=str(paths.sandbox_uploads_dir(thread_id)),
                read_only=False,
            ),
            PathMapping(
                container_path=f"{_USER_DATA_VIRTUAL_PREFIX}/outputs",
                local_path=str(paths.sandbox_outputs_dir(thread_id)),
                read_only=False,
            ),
            PathMapping(
                container_path=_ACP_WORKSPACE_VIRTUAL_PREFIX,
                local_path=str(paths.acp_workspace_dir(thread_id)),
                read_only=False,
            ),
        ]

    @staticmethod
    def _projection_mapping(available_skills: Collection[str] | None):
        from deerflow.config import get_app_config
        from deerflow.skills.projection import get_skill_projection

        projection = get_skill_projection(available_skills)
        mapping = PathMapping(
            container_path=get_app_config().skills.container_path,
            local_path=str(projection.path),
            read_only=True,
        )
        return projection, mapping

    def acquire(
        self,
        thread_id: str | None = None,
        *,
        available_skills: Collection[str] | None = None,
    ) -> str:
        global _singleton
        projection, skills_mapping = self._projection_mapping(available_skills)
        if thread_id is None:
            sandbox_id = f"local:global:{projection.revision}"
            with self._lock:
                if self._generic_sandbox is None or self._generic_sandbox.id != sandbox_id:
                    self._generic_sandbox = LocalSandbox(
                        sandbox_id,
                        path_mappings=[*self._path_mappings, skills_mapping],
                    )
                    _singleton = self._generic_sandbox
                return self._generic_sandbox.id

        cache_key = f"{thread_id}:{projection.revision}"
        with self._lock:
            cached = self._thread_sandboxes.get(cache_key)
            if cached is not None:
                self._thread_sandboxes.move_to_end(cache_key)
                return cached.id

        new_mappings = [*self._path_mappings, skills_mapping, *self._build_thread_path_mappings(thread_id)]

        with self._lock:
            cached = self._thread_sandboxes.get(cache_key)
            if cached is None:
                cached = LocalSandbox(f"local:{thread_id}:{projection.revision}", path_mappings=new_mappings)
                self._thread_sandboxes[cache_key] = cached
                self._evict_until_within_cap_locked()
            else:
                self._thread_sandboxes.move_to_end(cache_key)
            return cached.id

    def _evict_until_within_cap_locked(self) -> None:
        """LRU-evict cached thread sandboxes once the cap is exceeded."""
        while len(self._thread_sandboxes) > self._max_cached_threads:
            evicted_thread_id, _ = self._thread_sandboxes.popitem(last=False)
            logger.info(
                "Evicting LocalSandbox cache entry for thread %s (cap=%d)",
                evicted_thread_id,
                self._max_cached_threads,
            )

    def get(self, sandbox_id: str) -> Sandbox | None:
        if sandbox_id == "local" or sandbox_id.startswith("local:global:"):
            with self._lock:
                generic = self._generic_sandbox
            if generic is None:
                self.acquire()
                with self._lock:
                    return self._generic_sandbox
            return generic
        if isinstance(sandbox_id, str) and sandbox_id.startswith("local:"):
            cache_key = sandbox_id[len("local:") :]
            with self._lock:
                cached = self._thread_sandboxes.get(cache_key)
                if cached is not None:
                    self._thread_sandboxes.move_to_end(cache_key)
                return cached
        return None

    def release(self, sandbox_id: str) -> None:
        # LocalSandbox has no resources to release. Keep cached instances so
        # agent-authored path reverse resolution survives between turns.
        # Note: This method is intentionally not called by SandboxMiddleware
        # to allow sandbox reuse across multiple turns in a thread.
        pass

    def release_thread(self, thread_id: str) -> None:
        """Drop every cached Skill-revision view for one thread."""
        prefix = f"{thread_id}:"
        with self._lock:
            keys = [key for key in self._thread_sandboxes if key.startswith(prefix)]
            for key in keys:
                self._thread_sandboxes.pop(key, None)

    def active_skill_revisions(self) -> set[str]:
        with self._lock:
            sandbox_ids = [
                sandbox.id for sandbox in self._thread_sandboxes.values()
            ]
            if self._generic_sandbox is not None:
                sandbox_ids.append(self._generic_sandbox.id)
        return {
            sandbox_id.rsplit(":", 1)[-1]
            for sandbox_id in sandbox_ids
            if sandbox_id.count(":") >= 2
        }

    def reset(self) -> None:
        """Drop all cached LocalSandbox instances."""
        global _singleton
        with self._lock:
            self._generic_sandbox = None
            self._thread_sandboxes.clear()
            _singleton = None

    def shutdown(self) -> None:
        self.reset()
