"""Atomic file storage for single-user Skill evolution state."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from deerflow.config import get_app_config
from deerflow.config.paths import get_paths

from .models import EvolutionSignal, RevisionManifest, SkillProposal


_SIGNAL_ID_RE = re.compile(r"^s_[A-Za-z0-9_-]{1,128}$")


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def hash_skill_tree(path: Path | None) -> str | None:
    """Return a stable digest of every file in a Skill tree."""
    if path is None or not path.exists():
        return None
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*"), key=lambda value: value.relative_to(path).as_posix()):
        if item.is_symlink():
            raise ValueError(f"Symbolic links are not allowed in skills: {item}")
        if not item.is_file():
            continue
        relative = item.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def list_tree_files(path: Path | None) -> list[str]:
    if path is None or not path.exists():
        return []
    return sorted(item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file())


_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: dict[str, threading.RLock] = {}


def _root_lock(root: Path) -> threading.RLock:
    key = str(root.resolve())
    with _LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(key, threading.RLock())


class FileEvolutionStore:
    """File-backed Proposal, Revision and catalog state store.

    The deployment is single-user and uses one writer process.  The in-process
    re-entrant lock protects concurrent API/tool calls; atomic replaces protect
    readers from partially-written JSON files.
    """

    def __init__(self, root: str | Path | None = None):
        if root is None:
            raw = Path(get_app_config().skill_evolution.storage_path)
            root = raw if raw.is_absolute() else get_paths().base_dir / raw
        self.root = Path(root).resolve()
        self.lock = _root_lock(self.root)

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def proposals_dir(self) -> Path:
        return self.root / "proposals"

    @property
    def revisions_dir(self) -> Path:
        return self.root / "revisions"

    @property
    def signals_dir(self) -> Path:
        return self.root / "signals"

    @property
    def audit_path(self) -> Path:
        return self.root / "audit.jsonl"

    def ensure_layout(self) -> None:
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        self.revisions_dir.mkdir(parents=True, exist_ok=True)
        self.signals_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            temp_path = Path(handle.name)
        os.replace(temp_path, path)

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp") as handle:
            handle.write(content)
            temp_path = Path(handle.name)
        os.replace(temp_path, path)

    def read_state(self) -> dict[str, Any]:
        with self.lock:
            if not self.state_path.exists():
                return {
                    "schema_version": 1,
                    "catalog_version": 0,
                    "active_revisions": {},
                    "observations": {},
                    "probations": {},
                    "last_updated_at": None,
                }
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Invalid Skill evolution state at {self.state_path}: {exc}") from exc
            data.setdefault("schema_version", 1)
            data.setdefault("catalog_version", 0)
            data.setdefault("active_revisions", {})
            data.setdefault("observations", {})
            data.setdefault("probations", {})
            return data

    def write_state(self, state: dict[str, Any]) -> None:
        state = {**state, "last_updated_at": utc_now_iso()}
        self._atomic_write_json(self.state_path, state)

    def get_catalog_version(self) -> int:
        return int(self.read_state().get("catalog_version", 0))

    def get_active_revision(self, name: str) -> int | None:
        raw = self.read_state().get("active_revisions", {}).get(name)
        return int(raw) if raw is not None else None

    def bump_catalog(self, *, actor: str, action: str, details: dict[str, Any] | None = None) -> int:
        with self.lock:
            state = self.read_state()
            state["catalog_version"] = int(state.get("catalog_version", 0)) + 1
            self.write_state(state)
            self.append_audit(actor=actor, action=action, details={"catalog_version": state["catalog_version"], **(details or {})})
            return int(state["catalog_version"])

    def append_audit(self, *, actor: str, action: str, details: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            payload = {"ts": utc_now_iso(), "actor": actor, "action": action, "details": details or {}}
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str))
                handle.write("\n")

    def proposal_dir(self, proposal_id: str) -> Path:
        return self.proposals_dir / proposal_id

    def proposal_candidate_dir(self, proposal_id: str, skill_name: str) -> Path:
        return self.proposal_dir(proposal_id) / "candidate" / skill_name

    def signal_path(self, signal_id: str) -> Path:
        if not _SIGNAL_ID_RE.fullmatch(signal_id):
            raise ValueError("Invalid evolution signal id.")
        return self.signals_dir / f"{signal_id}.json"

    def save_proposal(self, proposal: SkillProposal) -> None:
        with self.lock:
            self.ensure_layout()
            self._atomic_write_json(self.proposal_dir(proposal.id) / "proposal.json", proposal.model_dump(mode="json"))

    def load_proposal(self, proposal_id: str) -> SkillProposal:
        path = self.proposal_dir(proposal_id) / "proposal.json"
        if not path.exists():
            raise FileNotFoundError(f"Skill proposal '{proposal_id}' not found.")
        try:
            return SkillProposal.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Invalid Skill proposal '{proposal_id}': {exc}") from exc

    def list_proposals(
        self,
        *,
        status: str | None = None,
        include_archived: bool = True,
        archived_only: bool = False,
    ) -> list[SkillProposal]:
        if not self.proposals_dir.exists():
            return []
        proposals: list[SkillProposal] = []
        for path in self.proposals_dir.glob("*/proposal.json"):
            try:
                proposal = SkillProposal.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if archived_only and proposal.archived_at is None:
                continue
            if not include_archived and proposal.archived_at is not None:
                continue
            if status is None or proposal.status == status:
                proposals.append(proposal)
        proposals.sort(key=lambda item: item.created_at, reverse=True)
        return proposals

    def save_proposal_diff(self, proposal_id: str, content: str) -> None:
        with self.lock:
            self._atomic_write_text(self.proposal_dir(proposal_id) / "diff.patch", content)

    def read_proposal_diff(self, proposal_id: str) -> str:
        path = self.proposal_dir(proposal_id) / "diff.patch"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def save_signal(self, signal: EvolutionSignal) -> None:
        with self.lock:
            self.ensure_layout()
            self._atomic_write_json(self.signal_path(signal.id), signal.model_dump(mode="json"))

    def load_signal(self, signal_id: str) -> EvolutionSignal:
        path = self.signal_path(signal_id)
        if not path.exists():
            raise FileNotFoundError(f"Evolution signal '{signal_id}' not found.")
        try:
            return EvolutionSignal.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Invalid evolution signal '{signal_id}': {exc}") from exc

    def delete_signal(self, signal_id: str) -> EvolutionSignal:
        """Delete one durable signal and return its final stored value."""
        with self.lock:
            signal = self.load_signal(signal_id)
            self.signal_path(signal_id).unlink()
            return signal

    def list_signals(self, *, status: str | None = None) -> list[EvolutionSignal]:
        if not self.signals_dir.exists():
            return []
        signals: list[EvolutionSignal] = []
        for path in self.signals_dir.glob("*.json"):
            try:
                signal = EvolutionSignal.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if status is None or signal.status == status:
                signals.append(signal)
        signals.sort(key=lambda item: item.created_at, reverse=True)
        return signals

    def register_observation(
        self,
        *,
        fingerprint: str,
        summary: str,
        window_days: int,
        cooldown_hours: int,
    ) -> tuple[int, bool]:
        """Record a task fingerprint and return recurrence count and cooldown state."""
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=window_days)
        with self.lock:
            state = self.read_state()
            observations = state.setdefault("observations", {})
            retained: dict[str, Any] = {}
            for key, value in observations.items():
                try:
                    last_seen = datetime.fromisoformat(str(value.get("last_seen")))
                except (TypeError, ValueError):
                    continue
                if last_seen >= cutoff:
                    retained[key] = value
            record = retained.get(fingerprint, {})
            occurrences: list[datetime] = []
            for raw_seen in record.get("occurrences", []):
                try:
                    seen_at = datetime.fromisoformat(str(raw_seen))
                except (TypeError, ValueError):
                    continue
                if seen_at >= cutoff:
                    occurrences.append(seen_at)
            # Migrate the original aggregate-only record conservatively. Its
            # exact historical distribution is unknown, so retain one recent
            # observation rather than keeping an unbounded lifetime count.
            if not occurrences and record.get("last_seen"):
                try:
                    previous_seen = datetime.fromisoformat(str(record["last_seen"]))
                    if previous_seen >= cutoff:
                        occurrences.append(previous_seen)
                except (TypeError, ValueError):
                    pass
            occurrences.append(now)
            # Bound state growth even when configured thresholds are high.
            occurrences = occurrences[-1_000:]
            count = len(occurrences)
            last_signal_at = record.get("last_signal_at")
            cooling_down = False
            if last_signal_at and cooldown_hours > 0:
                try:
                    cooling_down = datetime.fromisoformat(str(last_signal_at)) > now - timedelta(hours=cooldown_hours)
                except ValueError:
                    cooling_down = False
            retained[fingerprint] = {
                "count": count,
                "first_seen": record.get("first_seen") or now.isoformat(),
                "last_seen": now.isoformat(),
                "last_signal_at": last_signal_at,
                "summary": summary[:500],
                "occurrences": [value.isoformat() for value in occurrences],
            }
            state["observations"] = retained
            self.write_state(state)
            return count, cooling_down

    def mark_observation_signaled(self, fingerprint: str) -> None:
        with self.lock:
            state = self.read_state()
            record = state.setdefault("observations", {}).get(fingerprint)
            if record is None:
                return
            record["last_signal_at"] = utc_now_iso()
            self.write_state(state)

    def get_probations(self) -> dict[str, dict[str, Any]]:
        return dict(self.read_state().get("probations", {}))

    def set_probation(self, name: str, probation: dict[str, Any] | None) -> None:
        with self.lock:
            state = self.read_state()
            probations = state.setdefault("probations", {})
            if probation is None:
                probations.pop(name, None)
            else:
                probations[name] = probation
            self.write_state(state)

    def next_revision(self, name: str) -> int:
        root = self.revisions_dir / name
        versions = [int(path.name) for path in root.iterdir() if path.is_dir() and path.name.isdigit()] if root.exists() else []
        return max(versions, default=0) + 1

    def revision_dir(self, name: str, version: int) -> Path:
        return self.revisions_dir / name / str(version)

    def write_revision(self, manifest: RevisionManifest, snapshot: Path | None) -> None:
        with self.lock:
            destination = self.revision_dir(manifest.skill_name, manifest.version)
            if destination.exists():
                raise FileExistsError(f"Revision {manifest.skill_name}@{manifest.version} already exists.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp_dir = destination.parent / f".revision-{manifest.version}-{uuid.uuid4().hex}"
            temp_dir.mkdir(parents=True)
            try:
                self._atomic_write_json(temp_dir / "manifest.json", manifest.model_dump(mode="json"))
                if snapshot is not None:
                    shutil.copytree(snapshot, temp_dir / "snapshot", symlinks=True)
                temp_dir.replace(destination)
            except BaseException:
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise

    def load_revision(self, name: str, version: int) -> tuple[RevisionManifest, Path | None]:
        root = self.revision_dir(name, version)
        manifest_path = root / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"Revision '{name}@{version}' not found.")
        manifest = RevisionManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        snapshot = root / "snapshot"
        return manifest, snapshot if snapshot.exists() else None

    def list_revisions(self, name: str) -> list[RevisionManifest]:
        root = self.revisions_dir / name
        if not root.exists():
            return []
        manifests: list[RevisionManifest] = []
        for path in root.glob("*/manifest.json"):
            try:
                manifests.append(RevisionManifest.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        manifests.sort(key=lambda item: item.version, reverse=True)
        return manifests

    def bootstrap_active_skill(self, name: str, skill_dir: Path, *, actor: str = "system") -> int | None:
        with self.lock:
            current = self.get_active_revision(name)
            if current is not None:
                return current
            if not skill_dir.exists():
                return None
            version = self.next_revision(name)
            manifest = RevisionManifest(
                skill_name=name,
                version=version,
                created_at=utc_now_iso(),
                actor=actor,
                action="bootstrap",
                sha256=hash_skill_tree(skill_dir),
            )
            self.write_revision(manifest, skill_dir)
            state = self.read_state()
            state.setdefault("active_revisions", {})[name] = version
            self.write_state(state)
            self.append_audit(actor=actor, action="skill.bootstrap", details={"skill_name": name, "revision": version})
            return version


def build_tree_diff(before: Path | None, after: Path | None) -> str:
    """Build a unified, file-by-file diff for Admin review."""
    before_files = set(list_tree_files(before))
    after_files = set(list_tree_files(after))
    chunks: list[str] = []
    for relative in sorted(before_files | after_files):
        old_path = before / relative if before is not None and relative in before_files else None
        new_path = after / relative if after is not None and relative in after_files else None
        old_lines = old_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if old_path else []
        new_lines = new_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if new_path else []
        chunks.extend(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{relative}" if old_path else "/dev/null",
                tofile=f"b/{relative}" if new_path else "/dev/null",
            )
        )
    return "".join(chunks)


def changed_tree_files(before: Path | None, after: Path | None) -> list[str]:
    changed: list[str] = []
    for relative in sorted(set(list_tree_files(before)) | set(list_tree_files(after))):
        old = (before / relative).read_bytes() if before is not None and (before / relative).is_file() else None
        new = (after / relative).read_bytes() if after is not None and (after / relative).is_file() else None
        if old != new:
            changed.append(relative)
    return changed


def get_evolution_store() -> FileEvolutionStore:
    return FileEvolutionStore()
