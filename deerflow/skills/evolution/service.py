"""Review-first orchestration for Skill proposals and Admin publications."""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from deerflow.config import get_app_config
from deerflow.skills.manager import (
    ALLOWED_SUPPORT_SUBDIRS,
    atomic_write,
    custom_skill_exists,
    get_custom_skill_dir,
    public_skill_exists,
    validate_skill_markdown_content,
    validate_skill_name,
)
from deerflow.skills.security_scanner import ScanResult, scan_skill_content, static_scan_skill_content

from .evaluator import AutoPatchEvaluator
from .generator import SkillCandidateGenerator
from .models import EvolutionSignal, ProposalTrigger, SkillProposal
from .publisher import SkillPublishConflict, SkillPublisher
from .store import (
    FileEvolutionStore,
    build_tree_diff,
    changed_tree_files,
    hash_skill_tree,
    list_tree_files,
    utc_now_iso,
)


class CandidateValidationError(ValueError):
    def __init__(self, message: str, scans: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.scans = scans or []


def _scan_payload(location: str, result: ScanResult) -> dict[str, Any]:
    return {
        "location": location,
        "decision": result.decision,
        "reason": result.reason,
        "source": result.source,
        "findings": list(result.findings),
    }


class SkillEvolutionService:
    """Create proposals and publish only after explicit Admin approval."""

    def __init__(self, store: FileEvolutionStore | None = None):
        self.store = store or FileEvolutionStore()
        self.publisher = SkillPublisher(self.store)
        self.generator = SkillCandidateGenerator()
        self.evaluator = AutoPatchEvaluator(self.store)

    @staticmethod
    def _risk_for(action: str, path: str | None = None) -> str:
        if action in {"delete", "remove_file"} or (path or "").replace("\\", "/").startswith("scripts/"):
            return "high"
        if action in {"create", "edit", "write_file"}:
            return "medium"
        return "low"

    @staticmethod
    def _safe_candidate_support_path(candidate_dir: Path, relative_path: str) -> Path:
        if not relative_path or relative_path.endswith(("/", "\\")):
            raise ValueError("Supporting file path must include a filename.")
        relative = Path(relative_path)
        if relative.is_absolute() or any(part in {"", ".."} for part in relative.parts):
            raise ValueError("Supporting file path must be relative and stay inside the Skill directory.")
        if not relative.parts or relative.parts[0] not in ALLOWED_SUPPORT_SUBDIRS:
            raise ValueError(f"Supporting files must live under one of: {', '.join(sorted(ALLOWED_SUPPORT_SUBDIRS))}.")
        target = (candidate_dir / relative).resolve()
        allowed = (candidate_dir / relative.parts[0]).resolve()
        try:
            target.relative_to(allowed)
        except ValueError as exc:
            raise ValueError("Supporting file path escapes its allowed directory.") from exc
        return target

    @staticmethod
    def _clone_active(name: str, candidate_dir: Path) -> Path:
        source = get_custom_skill_dir(name)
        if not source.exists():
            raise FileNotFoundError(f"Custom skill '{name}' not found.")
        shutil.copytree(source, candidate_dir, symlinks=True)
        return source

    def _build_candidate(
        self,
        *,
        proposal_id: str,
        action: str,
        name: str,
        content: str | None,
        path: str | None,
        find: str | None,
        replace: str | None,
        expected_count: int | None,
    ) -> tuple[Path | None, Path | None]:
        active_dir = get_custom_skill_dir(name)
        candidate_dir = self.store.proposal_candidate_dir(proposal_id, name)

        if action == "create":
            if custom_skill_exists(name):
                raise ValueError(f"Custom skill '{name}' already exists.")
            if public_skill_exists(name):
                raise ValueError(f"'{name}' is a built-in skill. Use a distinct custom skill name.")
            if content is None:
                raise ValueError("content is required for create.")
            candidate_dir.mkdir(parents=True, exist_ok=False)
            atomic_write(candidate_dir / "SKILL.md", content)
            return None, candidate_dir

        if action == "delete":
            if not active_dir.exists():
                raise FileNotFoundError(f"Custom skill '{name}' not found.")
            return active_dir, None

        self._clone_active(name, candidate_dir)
        if action == "edit":
            if content is None:
                raise ValueError("content is required for edit.")
            atomic_write(candidate_dir / "SKILL.md", content)
        elif action == "patch":
            if find is None or replace is None:
                raise ValueError("find and replace are required for patch.")
            skill_file = candidate_dir / "SKILL.md"
            previous = skill_file.read_text(encoding="utf-8")
            occurrences = previous.count(find)
            if occurrences == 0:
                raise ValueError("Patch target not found in SKILL.md.")
            if expected_count is not None and occurrences != expected_count:
                raise ValueError(f"Expected {expected_count} replacements but found {occurrences}.")
            replacement_count = expected_count if expected_count is not None else 1
            atomic_write(skill_file, previous.replace(find, replace, replacement_count))
        elif action == "write_file":
            if path is None or content is None:
                raise ValueError("path and content are required for write_file.")
            atomic_write(self._safe_candidate_support_path(candidate_dir, path), content)
        elif action == "remove_file":
            if path is None:
                raise ValueError("path is required for remove_file.")
            target = self._safe_candidate_support_path(candidate_dir, path)
            if not target.is_file():
                raise FileNotFoundError(f"Supporting file '{path}' not found for skill '{name}'.")
            target.unlink()
        else:
            raise ValueError(f"Unsupported action '{action}'.")
        return active_dir, candidate_dir

    async def _validate_candidate(self, name: str, candidate_dir: Path | None, *, use_llm: bool) -> list[dict[str, Any]]:
        if candidate_dir is None:
            return []
        config = get_app_config().skill_evolution
        limits = config.candidate_limits
        files = list_tree_files(candidate_dir)
        if len(files) > limits.max_files:
            raise CandidateValidationError(f"Candidate contains {len(files)} files; limit is {limits.max_files}.")

        total_bytes = 0
        scans: list[dict[str, Any]] = []
        skill_file = candidate_dir / "SKILL.md"
        if not skill_file.is_file():
            raise CandidateValidationError("Candidate must contain SKILL.md.")
        try:
            validate_skill_markdown_content(name, skill_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise CandidateValidationError(str(exc)) from exc

        for item in sorted(candidate_dir.rglob("*")):
            if item.is_symlink():
                raise CandidateValidationError(f"Symbolic links are not allowed: {item.relative_to(candidate_dir).as_posix()}.")
            if not item.is_file():
                continue
            relative = item.relative_to(candidate_dir)
            if relative != Path("SKILL.md") and relative.name == "SKILL.md":
                raise CandidateValidationError(f"Nested SKILL.md is not allowed: {relative.as_posix()}.")
            if relative != Path("SKILL.md") and (not relative.parts or relative.parts[0] not in ALLOWED_SUPPORT_SUBDIRS):
                raise CandidateValidationError(f"Unsupported candidate path: {relative.as_posix()}.")
            size = item.stat().st_size
            total_bytes += size
            if size > limits.max_file_bytes:
                raise CandidateValidationError(f"Candidate file '{relative.as_posix()}' exceeds {limits.max_file_bytes} bytes.")
            try:
                text = item.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise CandidateValidationError(f"Candidate file '{relative.as_posix()}' must be UTF-8 text.") from exc
            location = f"{name}/{relative.as_posix()}"
            executable = bool(relative.parts and relative.parts[0] == "scripts")
            result = await scan_skill_content(text, executable=executable, location=location) if use_llm else static_scan_skill_content(text, executable=executable, location=location)
            scans.append(_scan_payload(location, result))
            if result.decision == "block":
                raise CandidateValidationError(result.reason, scans=scans)

        if total_bytes > limits.max_total_bytes:
            raise CandidateValidationError(f"Candidate size {total_bytes} exceeds {limits.max_total_bytes} bytes.", scans=scans)
        return scans

    async def create_proposal(
        self,
        *,
        action: str,
        name: str,
        content: str | None = None,
        path: str | None = None,
        find: str | None = None,
        replace: str | None = None,
        expected_count: int | None = None,
        reason: str | None = None,
        thread_id: str | None = None,
        trigger_type: str = "agent_request",
        trigger_summary: str | None = None,
        origin: str = "manual_agent",
        author: str | None = None,
    ) -> SkillProposal:
        if not get_app_config().skill_evolution.enabled:
            raise ValueError("Skill evolution is disabled.")
        if get_app_config().skill_evolution.mode not in {"review", "auto_patch"}:
            raise ValueError("Unsupported Skill evolution mode.")
        name = validate_skill_name(name)
        if action not in {"create", "edit", "patch", "delete", "write_file", "remove_file"}:
            raise ValueError(f"Unsupported action '{action}'.")
        if origin not in {"manual_agent", "automatic"}:
            raise ValueError(f"Unsupported proposal origin '{origin}'.")

        proposal_id = f"p_{uuid.uuid4().hex}"
        now = utc_now_iso()
        proposal = SkillProposal(
            id=proposal_id,
            status="generating",
            action=action,
            skill_name=name,
            file_path=path,
            reason=(reason or "").strip(),
            trigger=ProposalTrigger(
                type=trigger_type,
                thread_id=thread_id,
                summary=(trigger_summary if trigger_summary is not None else reason or "").strip(),
            ),
            author=author or ("system" if origin == "automatic" else "agent"),
            origin=origin,
            base_revision=None,
            base_sha256=None,
            risk=self._risk_for(action, path),
            created_at=now,
            updated_at=now,
        )
        self.store.save_proposal(proposal)

        try:
            active_dir = get_custom_skill_dir(name)
            if active_dir.exists():
                proposal.base_revision = self.store.bootstrap_active_skill(name, active_dir, actor="system")
                proposal.base_sha256 = hash_skill_tree(active_dir)
            before, candidate = self._build_candidate(
                proposal_id=proposal_id,
                action=action,
                name=name,
                content=content,
                path=path,
                find=find,
                replace=replace,
                expected_count=expected_count,
            )
            proposal.status = "validating"
            proposal.updated_at = utc_now_iso()
            self.store.save_proposal(proposal)
            proposal.scans = await self._validate_candidate(name, candidate, use_llm=True)
            proposal.candidate_sha256 = hash_skill_tree(candidate)
            proposal.changed_files = changed_tree_files(before, candidate)
            proposal.status = "pending_review"
            proposal.updated_at = utc_now_iso()
            self.store.save_proposal_diff(proposal_id, build_tree_diff(before, candidate))
            self.store.save_proposal(proposal)
            self.store.append_audit(
                actor=proposal.author,
                action="proposal.created",
                details={
                    "proposal_id": proposal.id,
                    "skill_name": name,
                    "proposal_action": action,
                    "risk": proposal.risk,
                    "thread_id": thread_id,
                    "origin": origin,
                    "trigger_type": trigger_type,
                },
            )
            return proposal
        except Exception as exc:
            proposal.status = "failed"
            proposal.error = str(exc)
            proposal.updated_at = utc_now_iso()
            if isinstance(exc, CandidateValidationError):
                proposal.scans = exc.scans
            self.store.save_proposal(proposal)
            self.store.append_audit(actor=proposal.author, action="proposal.failed", details={"proposal_id": proposal.id, "error": str(exc), "origin": origin})
            raise ValueError(f"Skill proposal '{proposal.id}' failed: {exc}") from exc

    async def _publish_pending_proposal(
        self,
        proposal: SkillProposal,
        *,
        actor: str,
        note: str | None,
        auto_published: bool,
        downgrade_on_error: bool = False,
    ) -> SkillProposal:
        if proposal.status != "pending_review":
            raise ValueError(f"Proposal '{proposal.id}' is not pending review.")

        proposal.status = "publishing"
        proposal.updated_at = utc_now_iso()
        self.store.save_proposal(proposal)
        candidate = None if proposal.action == "delete" else self.store.proposal_candidate_dir(proposal.id, proposal.skill_name)
        try:
            proposal.scans = await self._validate_candidate(proposal.skill_name, candidate, use_llm=True)
            if auto_published and any(scan.get("decision") != "allow" for scan in proposal.scans):
                raise CandidateValidationError("Automatic publication requires every fresh security scan to allow the candidate.", scans=proposal.scans)
            result = self.publisher.publish(
                name=proposal.skill_name,
                candidate_dir=candidate,
                action=proposal.action,
                actor=actor,
                expected_sha256=proposal.base_sha256,
                enforce_expected=True,
                source_proposal_id=proposal.id,
                note=note,
                auto_published=auto_published,
            )
        except SkillPublishConflict as exc:
            proposal.status = "pending_review" if downgrade_on_error else "stale"
            proposal.error = str(exc)
            proposal.updated_at = utc_now_iso()
            if downgrade_on_error:
                proposal.evaluation = {
                    **proposal.evaluation,
                    "decision": "review",
                    "reason": f"Automatic publication conflict: {exc}",
                }
            self.store.save_proposal(proposal)
            if downgrade_on_error:
                return proposal
            raise
        except Exception as exc:
            proposal.status = "pending_review" if downgrade_on_error else "failed"
            proposal.error = str(exc)
            proposal.updated_at = utc_now_iso()
            if downgrade_on_error:
                proposal.evaluation = {
                    **proposal.evaluation,
                    "decision": "review",
                    "reason": f"Automatic publication failed safely: {exc}",
                }
            self.store.save_proposal(proposal)
            if downgrade_on_error:
                return proposal
            raise

        proposal.status = "published"
        proposal.published_revision = int(result["manifest"]["version"])
        proposal.reviewed_at = utc_now_iso()
        proposal.review_note = note
        proposal.error = None
        proposal.updated_at = proposal.reviewed_at
        self.store.save_proposal(proposal)
        audit_action = "proposal.auto_published" if auto_published else "proposal.approved"
        self.store.append_audit(
            actor=actor,
            action=audit_action,
            details={"proposal_id": proposal.id, "revision": proposal.published_revision},
        )
        return proposal

    async def approve_proposal(
        self,
        proposal_id: str,
        *,
        expected_base_sha256: str | None = None,
        note: str | None = None,
    ) -> SkillProposal:
        proposal = self.store.load_proposal(proposal_id)
        if proposal.status != "pending_review":
            raise ValueError(f"Proposal '{proposal_id}' is not pending review.")
        if expected_base_sha256 is not None and expected_base_sha256 != proposal.base_sha256:
            raise SkillPublishConflict("The approval request does not match the proposal base digest.")

        return await self._publish_pending_proposal(
            proposal,
            actor="admin",
            note=note,
            auto_published=False,
        )

    async def maybe_auto_publish(self, proposal: SkillProposal, signal: EvolutionSignal | None = None) -> SkillProposal:
        """Auto-publish an eligible patch; every non-allow outcome stays reviewable."""
        if proposal.status != "pending_review" or proposal.origin != "automatic":
            return proposal
        evaluation = await self.evaluator.evaluate(proposal, signal)
        proposal.evaluation = evaluation
        proposal.updated_at = utc_now_iso()
        self.store.save_proposal(proposal)
        if evaluation.get("decision") != "allow":
            return proposal
        return await self._publish_pending_proposal(
            proposal,
            actor="auto",
            note="Automatically published after deterministic and independent quality gates.",
            auto_published=True,
            downgrade_on_error=True,
        )

    async def process_signal(self, signal_id: str) -> EvolutionSignal:
        """Generate at most one Proposal from a durable automatic signal."""
        signal = self.store.load_signal(signal_id)
        if signal.status in {"proposal_created", "ignored"}:
            return signal
        signal.status = "processing"
        signal.updated_at = utc_now_iso()
        signal.error = None
        self.store.save_signal(signal)
        try:
            candidate = await self.generator.generate(signal)
            if candidate.action == "skip":
                signal.status = "ignored"
                signal.error = candidate.reason or None
                signal.updated_at = utc_now_iso()
                self.store.save_signal(signal)
                self.store.append_audit(
                    actor="system",
                    action="signal.ignored",
                    details={"signal_id": signal.id, "reason": candidate.reason},
                )
                return signal

            proposal = await self.create_proposal(
                action=candidate.action,
                name=str(candidate.skill_name),
                content=candidate.content,
                find=candidate.find,
                replace=candidate.replace,
                expected_count=candidate.expected_count,
                reason=candidate.reason,
                thread_id=signal.thread_id,
                trigger_type="automatic_signal",
                trigger_summary=", ".join(signal.trigger_types),
                origin="automatic",
                author="system",
            )
            proposal = await self.maybe_auto_publish(proposal, signal)
            signal.status = "proposal_created"
            signal.proposal_id = proposal.id
            signal.updated_at = utc_now_iso()
            self.store.save_signal(signal)
            return signal
        except Exception as exc:
            signal.status = "failed"
            signal.error = str(exc)
            signal.updated_at = utc_now_iso()
            self.store.save_signal(signal)
            self.store.append_audit(actor="system", action="signal.failed", details={"signal_id": signal.id, "error": str(exc)})
            raise

    def reject_proposal(self, proposal_id: str, *, note: str | None = None) -> SkillProposal:
        proposal = self.store.load_proposal(proposal_id)
        if proposal.status != "pending_review":
            raise ValueError(f"Proposal '{proposal_id}' is not pending review.")
        proposal.status = "rejected"
        proposal.reviewed_at = utc_now_iso()
        proposal.review_note = note
        proposal.updated_at = proposal.reviewed_at
        self.store.save_proposal(proposal)
        self.store.append_audit(actor="admin", action="proposal.rejected", details={"proposal_id": proposal.id, "note": note})
        return proposal

    async def publish_admin_change(
        self,
        *,
        action: str,
        name: str,
        content: str | None = None,
        path: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Publish a trusted Admin edit through the same revision transaction."""
        name = validate_skill_name(name)
        self.store.ensure_layout()
        staging_parent = self.store.root / ".admin-staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{name}-", dir=staging_parent) as temp_dir:
            candidate = Path(temp_dir) / name
            active = get_custom_skill_dir(name)
            candidate_or_none: Path | None
            if action == "create":
                if active.exists():
                    raise ValueError(f"Custom skill '{name}' already exists.")
                if public_skill_exists(name):
                    raise ValueError(f"'{name}' is a built-in skill. Create a custom skill with a distinct name.")
                if content is None:
                    raise ValueError("content is required for create.")
                candidate.mkdir(parents=True)
                atomic_write(candidate / "SKILL.md", content)
                candidate_or_none = candidate
            elif action == "delete":
                if not active.exists():
                    raise FileNotFoundError(f"Custom skill '{name}' not found.")
                candidate_or_none = None
            else:
                if not active.exists():
                    raise FileNotFoundError(f"Custom skill '{name}' not found.")
                shutil.copytree(active, candidate, symlinks=True)
                if action == "edit":
                    if content is None:
                        raise ValueError("content is required for edit.")
                    atomic_write(candidate / "SKILL.md", content)
                elif action == "write_file":
                    if path is None or content is None:
                        raise ValueError("path and content are required for write_file.")
                    atomic_write(self._safe_candidate_support_path(candidate, path), content)
                elif action == "remove_file":
                    if path is None:
                        raise ValueError("path is required for remove_file.")
                    target = self._safe_candidate_support_path(candidate, path)
                    if not target.is_file():
                        raise FileNotFoundError(f"Supporting file '{path}' not found for skill '{name}'.")
                    target.unlink()
                else:
                    raise ValueError(f"Unsupported Admin action '{action}'.")
                candidate_or_none = candidate

            scans = await self._validate_candidate(name, candidate_or_none, use_llm=False)
            result = self.publisher.publish(name=name, candidate_dir=candidate_or_none, action=action, actor="admin", note=note)
            result["scans"] = scans
            return result

    def status(self) -> dict[str, Any]:
        proposals = self.store.list_proposals()
        counts: dict[str, int] = {}
        for proposal in proposals:
            counts[proposal.status] = counts.get(proposal.status, 0) + 1
        config = get_app_config().skill_evolution
        signal_counts: dict[str, int] = {}
        for signal in self.store.list_signals():
            signal_counts[signal.status] = signal_counts.get(signal.status, 0) + 1
        return {
            "enabled": config.enabled,
            "mode": config.mode,
            "discovery_enabled": config.discovery.enabled,
            "catalog_version": self.store.get_catalog_version(),
            "proposal_counts": counts,
            "signal_counts": signal_counts,
            "probations": self.store.get_probations(),
            "storage_path": str(self.store.root),
        }
