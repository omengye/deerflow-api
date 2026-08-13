"""Read-only, content-addressed filesystem views of enabled Skills."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from deerflow.config.paths import get_paths
from deerflow.skills.loader import load_skills
from deerflow.skills.types import Skill

logger = logging.getLogger(__name__)

_projection_lock = threading.RLock()
_MAX_PROJECTION_BUILD_ATTEMPTS = 3
_PROJECTION_CACHE_MAX_ENTRIES = 128
_PROJECTION_GC_INTERVAL_SECONDS = 3600
_PROJECTION_RETENTION_SECONDS = 7 * 24 * 60 * 60
_PROJECTION_MIN_KEEP = 256
_projection_cache: OrderedDict[tuple[object, ...], SkillProjection] = OrderedDict()
_last_projection_gc = 0.0


class _SkillSourcesChanged(RuntimeError):
    """Internal retry signal for a concurrently published Skill tree."""


@dataclass(frozen=True)
class SkillProjection:
    """An immutable host directory mounted at the configured skills path."""

    path: Path
    revision: str
    skill_names: frozenset[str]


def _source_fingerprint(skills_root: Path) -> tuple[object, ...]:
    """Return a metadata-only invalidation key for Skill source files.

    Content hashing remains authoritative on cache misses.  The inexpensive
    stat snapshot avoids rereading every Skill byte on every sandbox access.
    """
    entries: list[tuple[str, int, int, int, int]] = []
    for category in ("public", "custom"):
        category_root = skills_root / category
        if not category_root.is_dir():
            continue
        for current_root, dir_names, file_names in os.walk(
            category_root, followlinks=False
        ):
            current = Path(current_root)
            dir_names[:] = sorted(
                name
                for name in dir_names
                if not name.startswith(".") and not (current / name).is_symlink()
            )
            for name in sorted(file_names):
                path = current / name
                if path.is_symlink():
                    continue
                try:
                    file_stat = path.stat()
                except FileNotFoundError:
                    # A concurrent publication will produce a different
                    # fingerprint on the retry/cache lookup.
                    continue
                entries.append(
                    (
                        path.relative_to(skills_root).as_posix(),
                        file_stat.st_size,
                        file_stat.st_mtime_ns,
                        file_stat.st_ctime_ns,
                        getattr(file_stat, "st_ino", 0),
                    )
                )

    try:
        from deerflow.config.extensions_config import ExtensionsConfig

        extensions_path = ExtensionsConfig.resolve_config_path()
        if extensions_path is not None:
            extensions_stat = extensions_path.stat()
            extensions_key: tuple[object, ...] = (
                str(extensions_path.resolve()),
                extensions_stat.st_size,
                extensions_stat.st_mtime_ns,
                getattr(extensions_stat, "st_ino", 0),
            )
        else:
            extensions_key = (None,)
    except (FileNotFoundError, OSError):
        extensions_key = (None,)
    return (*entries, ("extensions", *extensions_key))


def _cache_projection(key: tuple[object, ...], projection: SkillProjection) -> None:
    _projection_cache[key] = projection
    _projection_cache.move_to_end(key)
    while len(_projection_cache) > _PROJECTION_CACHE_MAX_ENTRIES:
        _projection_cache.popitem(last=False)


def _remove_projection_tree(path: Path) -> None:
    """Remove an old read-only projection on POSIX and Windows."""
    for child in path.rglob("*"):
        try:
            child.chmod(child.stat().st_mode | stat.S_IWUSR)
        except OSError:
            pass
    try:
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    except OSError:
        pass
    shutil.rmtree(path)


def prune_skill_projections(
    *,
    keep_revisions: Collection[str] = (),
    now: float | None = None,
    retention_seconds: float = _PROJECTION_RETENTION_SECONDS,
    min_keep: int = _PROJECTION_MIN_KEEP,
) -> list[str]:
    """Delete old, unreferenced content-addressed Skill views.

    The newest ``min_keep`` revisions are retained regardless of age.  This is
    deliberately at least as large as the in-process local/WSL sandbox caches,
    so periodic GC cannot remove a view still reachable through those caches.
    """
    root = get_paths().skill_projections_dir
    if not root.is_dir():
        return []
    current_time = time.time() if now is None else now
    keep = set(keep_revisions)
    try:
        from deerflow.sandbox.sandbox_provider import get_existing_sandbox_provider

        provider = get_existing_sandbox_provider()
        if provider is not None:
            keep.update(provider.active_skill_revisions())
    except Exception:
        # GC is opportunistic; a provider inspection failure must never make
        # deletion less conservative.
        logger.warning(
            "Skipping Skill projection GC because active revisions could not be read",
            exc_info=True,
        )
        return []
    projections: list[tuple[float, Path]] = []
    for path in root.iterdir():
        if (
            path.is_dir()
            and len(path.name) == 24
            and all(character in "0123456789abcdef" for character in path.name)
        ):
            try:
                projections.append((path.stat().st_mtime, path))
            except FileNotFoundError:
                continue
    projections.sort(key=lambda item: item[0], reverse=True)
    removed: list[str] = []
    for index, (modified_at, path) in enumerate(projections):
        if (
            path.name in keep
            or index < max(0, min_keep)
            or current_time - modified_at < retention_seconds
        ):
            continue
        try:
            _remove_projection_tree(path)
            removed.append(path.name)
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("Failed to prune Skill projection %s", path, exc_info=True)
    return removed


def _maybe_prune_skill_projections(current_revision: str) -> None:
    global _last_projection_gc
    now = time.monotonic()
    if now - _last_projection_gc < _PROJECTION_GC_INTERVAL_SECONDS:
        return
    _last_projection_gc = now
    removed = prune_skill_projections(keep_revisions={current_revision})
    if removed:
        logger.info("Pruned %d stale Skill projection(s)", len(removed))


def _iter_skill_files(skill: Skill, skills_root: Path):
    source = skill.skill_dir.resolve()
    try:
        source.relative_to(skills_root)
    except ValueError as exc:
        raise PermissionError(f"Skill {skill.name!r} resolves outside the configured skills root") from exc

    for current_root, dir_names, file_names in os.walk(source, followlinks=False):
        current = Path(current_root)
        safe_dirs: list[str] = []
        for name in sorted(dir_names):
            child = current / name
            if child.is_symlink():
                logger.warning("Ignoring symlinked directory in Skill %s: %s", skill.name, child)
                continue
            safe_dirs.append(name)
        dir_names[:] = safe_dirs

        for name in sorted(file_names):
            path = current / name
            if path.is_symlink():
                logger.warning("Ignoring symlinked file in Skill %s: %s", skill.name, path)
                continue
            resolved = path.resolve()
            try:
                resolved.relative_to(skills_root)
            except ValueError as exc:
                raise PermissionError(f"Skill file {path} resolves outside the configured skills root") from exc
            if resolved.is_file():
                yield path.relative_to(source), resolved


def _projection_revision(skills: list[Skill], skills_root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(b"deerflow-skill-projection-v1\0")
    for skill in skills:
        digest.update(f"{skill.name}\0{skill.category}\0{skill.relative_path.as_posix()}\0".encode())
        for relative, source in _iter_skill_files(skill, skills_root):
            digest.update(relative.as_posix().encode())
            digest.update(b"\0")
            with source.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()[:24]


def _staged_projection_revision(skills: list[Skill], staging: Path) -> str:
    """Hash the exact bytes copied into a staging projection.

    Source Skills may be published while a projection is being built.  The
    pre-build signature is only a fast-path hint; this second signature makes
    the directory name authoritative for the immutable snapshot that will
    actually be mounted.
    """
    digest = hashlib.sha256()
    digest.update(b"deerflow-skill-projection-v1\0")
    for skill in skills:
        digest.update(f"{skill.name}\0{skill.category}\0{skill.relative_path.as_posix()}\0".encode())
        source = staging / skill.category / skill.relative_path
        for current_root, dir_names, file_names in os.walk(source, followlinks=False):
            current = Path(current_root)
            dir_names[:] = sorted(
                name for name in dir_names if not (current / name).is_symlink()
            )
            for name in sorted(file_names):
                path = current / name
                if path.is_symlink() or not path.is_file():
                    continue
                digest.update(path.relative_to(source).as_posix().encode())
                digest.update(b"\0")
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                digest.update(b"\0")
    return digest.hexdigest()[:24]


def _make_tree_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            mode = path.stat().st_mode
            path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        except OSError:
            logger.debug("Could not make Skill projection path read-only: %s", path, exc_info=True)
    try:
        mode = root.stat().st_mode
        root.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    except OSError:
        logger.debug("Could not make Skill projection root read-only: %s", root, exc_info=True)


def _materialize_projection(
    destination: Path,
    *,
    revision: str,
    skills: list[Skill],
    skills_root: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".building-{revision}-{uuid.uuid4().hex}"
    try:
        (staging / "public").mkdir(parents=True)
        (staging / "custom").mkdir(parents=True)
        manifest: list[dict[str, str]] = []
        for skill in skills:
            target = staging / skill.category / skill.relative_path
            target.mkdir(parents=True, exist_ok=True)
            for relative, source in _iter_skill_files(skill, skills_root):
                output = target / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, output)
                shutil.copystat(source, output, follow_symlinks=True)
            manifest.append(
                {
                    "name": skill.name,
                    "category": skill.category,
                    "path": skill.relative_path.as_posix(),
                }
            )
        staged_revision = _staged_projection_revision(skills, staging)
        if staged_revision != revision:
            logger.info(
                "Skill sources changed while projection %s was being built; "
                "discarding the mixed snapshot and rescanning (%s)",
                revision,
                staged_revision,
            )
            raise _SkillSourcesChanged
        (staging / ".projection.json").write_text(
            json.dumps({"revision": revision, "skills": manifest}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            staging.replace(destination)
        except OSError:
            # Another process completed the same content-addressed view first.
            if not destination.is_dir():
                raise
            shutil.rmtree(staging, ignore_errors=True)
        _make_tree_read_only(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def get_skill_projection(available_skills: Collection[str] | None = None) -> SkillProjection:
    """Return the enabled Skill view intersected with an optional allowlist."""
    from deerflow.config import get_app_config

    config = get_app_config()
    skills_root = config.skills.get_skills_path().resolve()
    allowed = set(available_skills) if available_skills is not None else None
    with _projection_lock:
        for attempt in range(1, _MAX_PROJECTION_BUILD_ATTEMPTS + 1):
            fingerprint = _source_fingerprint(skills_root)
            cache_key: tuple[object, ...] = (
                str(skills_root),
                bool(getattr(config.skills, "enabled", True)),
                None if allowed is None else tuple(sorted(allowed)),
                fingerprint,
            )
            cached = _projection_cache.get(cache_key)
            if cached is not None and cached.path.is_dir():
                _projection_cache.move_to_end(cache_key)
                _maybe_prune_skill_projections(cached.revision)
                return cached
            enabled = (
                load_skills(skills_path=skills_root, use_config=False, enabled_only=True)
                if getattr(config.skills, "enabled", True)
                else []
            )
            selected = [
                skill
                for skill in enabled
                if allowed is None or skill.name in allowed
            ]
            revision = _projection_revision(selected, skills_root)
            destination = get_paths().skill_projections_dir / revision
            if destination.is_dir():
                projection = SkillProjection(
                    path=destination,
                    revision=revision,
                    skill_names=frozenset(skill.name for skill in selected),
                )
                _cache_projection(cache_key, projection)
                _maybe_prune_skill_projections(revision)
                return projection
            try:
                _materialize_projection(
                    destination,
                    revision=revision,
                    skills=selected,
                    skills_root=skills_root,
                )
            except _SkillSourcesChanged:
                if attempt == _MAX_PROJECTION_BUILD_ATTEMPTS:
                    raise RuntimeError(
                        "Skill sources kept changing while building a sandbox projection"
                    ) from None
                continue
            projection = SkillProjection(
                path=destination,
                revision=revision,
                skill_names=frozenset(skill.name for skill in selected),
            )
            # Cache only if the quick snapshot still describes the source
            # tree whose bytes were just verified by _materialize_projection.
            if _source_fingerprint(skills_root) == fingerprint:
                _cache_projection(cache_key, projection)
            _maybe_prune_skill_projections(revision)
            return projection
    raise AssertionError("unreachable")
