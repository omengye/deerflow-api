"""Transactional publication and rollback for active custom Skills."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

from deerflow.config import get_app_config
from deerflow.skills.manager import get_custom_skill_dir, get_custom_skills_dir, public_skill_exists

from .models import RevisionManifest
from .store import FileEvolutionStore, hash_skill_tree, utc_now_iso


class SkillPublishConflict(ValueError):
    """Raised when a proposal was based on an obsolete active Skill."""


class SkillPublisher:
    """The only service allowed to mutate the active custom Skill tree."""

    def __init__(self, store: FileEvolutionStore | None = None):
        self.store = store or FileEvolutionStore()

    @staticmethod
    def _refresh_prompt_cache() -> None:
        try:
            from deerflow.agents.lead_agent.prompt import clear_skills_system_prompt_cache

            clear_skills_system_prompt_cache()
        except Exception:
            # Publication has already committed at this point. The catalog
            # version still forces DeerFlowClient to recreate its agent.
            pass

    def publish(
        self,
        *,
        name: str,
        candidate_dir: Path | None,
        action: str,
        actor: str,
        expected_sha256: str | None = None,
        enforce_expected: bool = False,
        source_proposal_id: str | None = None,
        note: str | None = None,
        rollback_of: int | None = None,
        auto_published: bool = False,
    ) -> dict[str, Any]:
        active_dir = get_custom_skill_dir(name)
        custom_root = get_custom_skills_dir()
        deleted = candidate_dir is None

        with self.store.lock:
            if not active_dir.exists() and candidate_dir is not None and public_skill_exists(name):
                raise ValueError(f"'{name}' is a built-in skill. Use a distinct custom skill name.")

            current_sha = hash_skill_tree(active_dir)
            if enforce_expected and current_sha != expected_sha256:
                raise SkillPublishConflict(
                    f"Skill '{name}' changed after the proposal was created "
                    f"(expected {expected_sha256 or 'missing'}, found {current_sha or 'missing'})."
                )

            previous_revision = self.store.get_active_revision(name)
            if previous_revision is None and active_dir.exists():
                previous_revision = self.store.bootstrap_active_skill(name, active_dir, actor="system")
            version = self.store.next_revision(name)
            candidate_sha = hash_skill_tree(candidate_dir)
            manifest = RevisionManifest(
                skill_name=name,
                version=version,
                created_at=utc_now_iso(),
                actor=actor,
                action=action,
                previous_revision=previous_revision,
                source_proposal_id=source_proposal_id,
                sha256=candidate_sha,
                deleted=deleted,
                note=note,
                rollback_of=rollback_of,
            )
            self.store.write_revision(manifest, candidate_dir)

            staging = custom_root / f".publishing-{name}-{uuid.uuid4().hex}"
            backup = custom_root / f".backup-{name}-{uuid.uuid4().hex}"
            revision_root = self.store.revision_dir(name, version)
            try:
                if candidate_dir is not None:
                    shutil.copytree(candidate_dir, staging, symlinks=True)
                if active_dir.exists():
                    active_dir.replace(backup)
                if candidate_dir is not None:
                    staging.replace(active_dir)

                state = self.store.read_state()
                state.setdefault("active_revisions", {})[name] = version
                state["catalog_version"] = int(state.get("catalog_version", 0)) + 1
                probations = state.setdefault("probations", {})
                probation: dict[str, Any] | None = None
                if not deleted and action != "rollback":
                    monitoring = get_app_config().skill_evolution.monitoring
                    probation = {
                        "revision": version,
                        "previous_revision": previous_revision,
                        "remaining_uses": monitoring.probation_uses,
                        "consecutive_failures": 0,
                        "auto_published": auto_published,
                        "status": "probation",
                        "started_at": utc_now_iso(),
                        "last_observed_at": None,
                        "last_failure": None,
                    }
                    probations[name] = probation
                else:
                    probations.pop(name, None)
                self.store.write_state(state)
            except BaseException:
                if active_dir.exists():
                    shutil.rmtree(active_dir, ignore_errors=True)
                if backup.exists():
                    backup.replace(active_dir)
                shutil.rmtree(staging, ignore_errors=True)
                # The revision was never activated and is safe to discard.
                shutil.rmtree(revision_root, ignore_errors=True)
                raise
            finally:
                shutil.rmtree(staging, ignore_errors=True)

            shutil.rmtree(backup, ignore_errors=True)
            catalog_version = self.store.get_catalog_version()
            self.store.append_audit(
                actor=actor,
                action="skill.publish",
                details={
                    "skill_name": name,
                    "revision": version,
                    "catalog_version": catalog_version,
                    "proposal_id": source_proposal_id,
                    "publish_action": action,
                    "deleted": deleted,
                    "rollback_of": rollback_of,
                    "auto_published": auto_published,
                },
            )

        self._refresh_prompt_cache()
        return {
            "manifest": manifest.model_dump(mode="json"),
            "catalog_version": catalog_version,
            "probation": probation,
        }

    def rollback(self, name: str, version: int, *, actor: str = "admin", note: str | None = None) -> dict[str, Any]:
        target, snapshot = self.store.load_revision(name, version)
        return self.publish(
            name=name,
            candidate_dir=None if target.deleted else snapshot,
            action="rollback",
            actor=actor,
            note=note,
            rollback_of=version,
        )
