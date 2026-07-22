"""Persistence models for review-first Skill evolution."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ProposalStatus = Literal[
    "generating",
    "validating",
    "pending_review",
    "publishing",
    "published",
    "rejected",
    "failed",
    "stale",
]
ProposalAction = Literal["create", "edit", "patch", "delete", "write_file", "remove_file"]
RiskLevel = Literal["low", "medium", "high"]
SignalStatus = Literal["pending", "processing", "proposal_created", "ignored", "failed"]


class ProposalTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "agent_request"
    thread_id: str | None = None
    summary: str = ""


class SkillProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: ProposalStatus
    action: ProposalAction
    skill_name: str
    file_path: str | None = None
    reason: str = ""
    trigger: ProposalTrigger = Field(default_factory=ProposalTrigger)
    author: str = "agent"
    origin: Literal["manual_agent", "automatic"] = "manual_agent"
    base_revision: int | None = None
    base_sha256: str | None = None
    candidate_sha256: str | None = None
    risk: RiskLevel = "medium"
    changed_files: list[str] = Field(default_factory=list)
    scans: list[dict[str, Any]] = Field(default_factory=list)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    reviewed_at: str | None = None
    review_note: str | None = None
    published_revision: int | None = None
    error: str | None = None


class EvolutionSignal(BaseModel):
    """A sanitized, durable task-level improvement signal."""

    model_config = ConfigDict(extra="forbid")

    id: str
    status: SignalStatus = "pending"
    fingerprint: str
    trigger_types: list[str] = Field(default_factory=list)
    thread_id: str | None = None
    run_id: str | None = None
    user_summary: str = ""
    assistant_summary: str = ""
    tool_names: list[str] = Field(default_factory=list)
    tool_count: int = 0
    tool_error_count: int = 0
    recovered_error_count: int = 0
    unresolved_error_count: int = 0
    recurrence_count: int = 1
    skills_used: list[dict[str, Any]] = Field(default_factory=list)
    proposal_id: str | None = None
    created_at: str
    updated_at: str
    error: str | None = None


class RevisionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_name: str
    version: int
    created_at: str
    actor: str
    action: str
    previous_revision: int | None = None
    source_proposal_id: str | None = None
    sha256: str | None = None
    deleted: bool = False
    note: str | None = None
    rollback_of: int | None = None
