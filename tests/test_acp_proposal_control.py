from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from deerflow.acp import proposal_control
from deerflow.skills.evolution.models import SkillProposal
from deerflow.skills.evolution.store import FileEvolutionStore


def proposal() -> SkillProposal:
    return SkillProposal(
        id="p_test",
        status="pending_review",
        action="edit",
        skill_name="example",
        reason="Improve instructions",
        base_sha256="a" * 64,
        candidate_sha256="b" * 64,
        created_at="2026-08-15T00:00:00+00:00",
        updated_at="2026-08-15T00:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_list_get_and_reject_proposals(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileEvolutionStore(tmp_path / "evolution")
    item = proposal()
    store.save_proposal(item)
    store.save_proposal_diff(item.id, "--- a/SKILL.md\n+++ b/SKILL.md\n")
    monkeypatch.setattr(proposal_control, "get_evolution_store", lambda: store)

    listed = await proposal_control.handle_proposal_management_request(
        {"operation": "proposal.list", "status": "pending_review"}
    )
    assert listed["ok"] is True
    assert [value["id"] for value in listed["data"]["proposals"]] == [item.id]

    detail = await proposal_control.handle_proposal_management_request(
        {"operation": "proposal.get", "proposal_id": item.id}
    )
    assert detail["ok"] is True
    assert detail["data"]["diff"].startswith("--- a/SKILL.md")

    rejected = await proposal_control.handle_proposal_management_request(
        {
            "operation": "proposal.reject",
            "proposal_id": item.id,
            "note": "Not useful",
        }
    )
    assert rejected["ok"] is True
    assert rejected["data"]["proposal"]["status"] == "rejected"
    assert store.load_proposal(item.id).review_note == "Not useful"


@pytest.mark.asyncio
async def test_proposal_management_validates_ids_and_maps_conflicts(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileEvolutionStore(tmp_path / "evolution")
    store.save_proposal(proposal())
    monkeypatch.setattr(proposal_control, "get_evolution_store", lambda: store)

    invalid = await proposal_control.handle_proposal_management_request(
        {"operation": "proposal.get", "proposal_id": "../state"}
    )
    assert invalid == {
        "ok": False,
        "error": "Invalid Skill proposal id.",
        "code": "invalid_request",
    }

    conflict = await proposal_control.handle_proposal_management_request(
        {
            "operation": "proposal.approve",
            "proposal_id": "p_test",
            "expected_base_sha256": "c" * 64,
        }
    )
    assert conflict["ok"] is False
    assert conflict["code"] == "conflict"


@pytest.mark.asyncio
async def test_approve_refreshes_prompt_cache(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileEvolutionStore(tmp_path / "evolution")
    item = proposal()
    store.save_proposal(item)
    refreshed = False

    class FakeService:
        def __init__(self, actual_store):
            assert actual_store is store

        async def approve_proposal(self, proposal_id, **kwargs):
            assert proposal_id == item.id
            assert kwargs["actor"] == "desktop"
            item.status = "published"
            return item

    async def refresh() -> None:
        nonlocal refreshed
        refreshed = True

    monkeypatch.setattr(proposal_control, "get_evolution_store", lambda: store)
    monkeypatch.setattr(proposal_control, "SkillEvolutionService", FakeService)
    monkeypatch.setitem(
        sys.modules,
        "deerflow.agents.lead_agent.prompt",
        SimpleNamespace(refresh_skills_system_prompt_cache_async=refresh),
    )

    approved = await proposal_control.handle_proposal_management_request(
        {
            "operation": "proposal.approve",
            "proposal_id": item.id,
            "expected_base_sha256": item.base_sha256,
            "note": "Ship it",
        }
    )
    assert approved["ok"] is True
    assert approved["data"]["proposal"]["status"] == "published"
    assert refreshed is True
