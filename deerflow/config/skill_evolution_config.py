from typing import Literal

from pydantic import BaseModel, Field


class SkillEvolutionDiscoveryConfig(BaseModel):
    """Automatic-discovery recurrence, cooldown and quota controls."""

    enabled: bool = False
    min_tool_calls: int = Field(default=5, ge=1, le=1000)
    repeat_threshold: int = Field(default=2, ge=2, le=100)
    repeat_window_days: int = Field(default=30, ge=1, le=3650)
    cooldown_hours: int = Field(default=24, ge=0, le=8760)
    max_daily_proposals: int = Field(default=5, ge=1, le=1000)
    max_pending_proposals: int = Field(default=20, ge=1, le=10000)


class SkillEvolutionCandidateLimits(BaseModel):
    """Filesystem limits applied to every agent-created candidate."""

    max_files: int = Field(default=20, ge=1, le=1000)
    max_total_bytes: int = Field(default=524_288, ge=1024, le=100_000_000)
    max_file_bytes: int = Field(default=131_072, ge=256, le=10_000_000)


class SkillEvolutionAutoPatchConfig(BaseModel):
    """Safety envelope for low-risk automatic SKILL.md patches."""

    max_changed_lines: int = Field(default=40, ge=1, le=10_000)
    # These are explicit safety locks, not dormant feature toggles. Automatic
    # create/support-file/script/delete publication is outside this design.
    allow_create: Literal[False] = False
    allow_support_files: Literal[False] = False
    allow_scripts: Literal[False] = False
    allow_delete: Literal[False] = False


class SkillEvolutionMonitoringConfig(BaseModel):
    """Post-publication probation and rollback settings."""

    probation_uses: int = Field(default=3, ge=1, le=100)
    auto_rollback_consecutive_failures: int = Field(default=2, ge=1, le=100)


class SkillEvolutionConfig(BaseModel):
    """Configuration for agent-managed skill evolution."""

    enabled: bool = Field(
        default=False,
        description="Whether the agent can submit skill evolution proposals.",
    )
    mode: Literal["review", "auto_patch"] = Field(
        default="review",
        description="Publication mode. auto_patch remains bounded to eligible automatic patches.",
    )
    storage_path: str = Field(
        default="skill-evolution",
        min_length=1,
        description="Evolution state directory. Relative paths resolve under DEER_FLOW_HOME.",
    )
    generation_model_name: str | None = Field(
        default=None,
        description="Optional model used to generate automatic candidates. Defaults to the primary chat model.",
    )
    moderation_model_name: str | None = Field(
        default=None,
        description="Optional model name for skill security moderation. Defaults to the primary chat model.",
    )
    evaluation_model_name: str | None = Field(
        default=None,
        description="Optional model used for candidate quality evaluation.",
    )
    security_fail_closed: bool = Field(
        default=True,
        description="Block agent proposals when the LLM security scanner is unavailable or malformed.",
    )
    discovery: SkillEvolutionDiscoveryConfig = Field(default_factory=SkillEvolutionDiscoveryConfig)
    candidate_limits: SkillEvolutionCandidateLimits = Field(default_factory=SkillEvolutionCandidateLimits)
    auto_patch: SkillEvolutionAutoPatchConfig = Field(default_factory=SkillEvolutionAutoPatchConfig)
    monitoring: SkillEvolutionMonitoringConfig = Field(default_factory=SkillEvolutionMonitoringConfig)
