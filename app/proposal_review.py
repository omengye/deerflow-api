"""Shared Skill Proposal review operations for Admin and chat channels."""

from __future__ import annotations

from contextlib import contextmanager
import logging
from pathlib import Path
from typing import Iterator

from app.config import settings
from deerflow.config.app_config import AppConfig, pop_current_app_config, push_current_app_config
from deerflow.skills.evolution import SkillEvolutionService, get_evolution_store
from deerflow.skills.evolution.models import SkillProposal

logger = logging.getLogger(__name__)


def _config_path() -> Path:
    path = Path(settings.config_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


@contextmanager
def proposal_app_config_context() -> Iterator[None]:
    """Load the configured DeerFlow runtime for one review operation."""
    config = AppConfig.from_file(str(_config_path()))
    push_current_app_config(config)
    try:
        yield
    finally:
        pop_current_app_config()


async def refresh_skill_prompt_cache() -> None:
    """Best-effort refresh after a Proposal changes the active Skill tree."""
    try:
        from deerflow.agents.lead_agent.prompt import refresh_skills_system_prompt_cache_async

        await refresh_skills_system_prompt_cache_async()
    except Exception:
        logger.debug("Failed to refresh skills prompt cache after Skill change", exc_info=True)


def get_skill_proposal(proposal_id: str) -> tuple[SkillProposal, str]:
    with proposal_app_config_context():
        store = get_evolution_store()
        proposal = store.load_proposal(proposal_id)
        return proposal, store.read_proposal_diff(proposal_id)


def list_pending_proposals(thread_id: str | None = None) -> list[tuple[SkillProposal, str]]:
    with proposal_app_config_context():
        store = get_evolution_store()
        proposals = store.list_proposals(status="pending_review", include_archived=False)
        if thread_id is not None:
            proposals = [proposal for proposal in proposals if proposal.trigger.thread_id == thread_id]
        return [(proposal, store.read_proposal_diff(proposal.id)) for proposal in proposals]


def get_skill_catalog_version() -> int:
    with proposal_app_config_context():
        return get_evolution_store().get_catalog_version()


async def approve_skill_proposal(
    proposal_id: str,
    *,
    expected_base_sha256: str | None = None,
    note: str | None = None,
    actor: str = "admin",
) -> SkillProposal:
    with proposal_app_config_context():
        proposal = await SkillEvolutionService().approve_proposal(
            proposal_id,
            expected_base_sha256=expected_base_sha256,
            note=note,
            actor=actor,
        )
        await refresh_skill_prompt_cache()
        return proposal


def reject_skill_proposal(
    proposal_id: str,
    *,
    note: str | None = None,
    actor: str = "admin",
) -> SkillProposal:
    with proposal_app_config_context():
        return SkillEvolutionService().reject_proposal(proposal_id, note=note, actor=actor)
