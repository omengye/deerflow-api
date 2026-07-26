"""Configuration for the subagent system loaded from config.yaml."""

import logging

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class ModelSettingsConfig(BaseModel):
    """Generation parameter overrides passed straight through to the chat model constructor.

    Deliberately scoped to the handful of provider-agnostic sampling knobs
    (temperature, max_tokens) that make sense to tune per subagent. Anything
    provider-specific (e.g. top_p) belongs on the model's own `models:` entry
    in config.yaml instead, where `extra: allow` already lets that model
    declare arbitrary default kwargs. `extra: forbid` here is intentional and
    asymmetric with that: a typo in a per-agent override (e.g. `tempurature`)
    should fail loudly at config-load time instead of silently being dropped.
    """

    model_config = ConfigDict(extra="forbid")

    temperature: float | None = Field(
        default=None,
        ge=0,
        le=2,
        description="Sampling temperature override for this subagent (None = inherit)",
    )
    max_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Max output tokens override for this subagent (None = inherit)",
    )


class SubagentOverrideConfig(BaseModel):
    """Per-agent configuration overrides."""

    model_config = ConfigDict(extra="allow")

    timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        description="Timeout in seconds for this subagent (None = use global default)",
    )
    max_turns: int | None = Field(
        default=None,
        ge=1,
        description="Maximum turns for this subagent (None = use global or builtin default)",
    )
    model: str | None = Field(
        default=None,
        min_length=1,
        description="Model name for this subagent (None = inherit from parent agent)",
    )
    skills: list[str] | None = Field(
        default=None,
        description="Skill names whitelist for this subagent (None = inherit all enabled skills, [] = no skills)",
    )
    model_settings: ModelSettingsConfig | None = Field(
        default=None,
        description="Generation parameter overrides (temperature, max_tokens) for this subagent (None = inherit)",
    )
    thinking_enabled: bool | None = Field(
        default=None,
        description="Enable/disable thinking mode for this subagent (None = inherit from parent agent)",
    )
    reasoning_effort: str | None = Field(
        default=None,
        description="Reasoning effort override for this subagent, one of low/medium/high/xhigh (None = inherit)",
    )


class CustomSubagentConfig(BaseModel):
    """User-defined subagent type declared in config.yaml."""

    model_config = ConfigDict(extra="allow")

    description: str = Field(
        description="When the lead agent should delegate to this subagent",
    )
    system_prompt: str = Field(
        description="System prompt that guides the subagent's behavior",
    )
    tools: list[str] | None = Field(
        default=None,
        description="Tool names whitelist (None = inherit all tools from parent)",
    )
    disallowed_tools: list[str] | None = Field(
        default_factory=lambda: ["task", "ask_clarification", "present_files"],
        description="Tool names to deny",
    )
    skills: list[str] | None = Field(
        default=None,
        description="Skill names whitelist (None = inherit all enabled skills, [] = no skills)",
    )
    model: str = Field(
        default="inherit",
        description="Model to use - 'inherit' uses parent's model",
    )
    max_turns: int = Field(
        default=50,
        ge=1,
        description="Maximum number of agent turns before stopping",
    )
    timeout_seconds: int = Field(
        default=900,
        ge=1,
        description="Maximum execution time in seconds",
    )
    model_settings: ModelSettingsConfig | None = Field(
        default=None,
        description="Generation parameter overrides (temperature, max_tokens) for this subagent (None = model defaults)",
    )
    thinking_enabled: bool | None = Field(
        default=None,
        description="Enable/disable thinking mode for this subagent (None = model/factory default)",
    )
    reasoning_effort: str | None = Field(
        default=None,
        description="Reasoning effort for this subagent, one of low/medium/high/xhigh (None = model/factory default)",
    )


class SubagentsAppConfig(BaseModel):
    """Configuration for the subagent system."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = Field(
        default=True,
        description="Whether subagents are available when the runtime enables multi-agent mode",
    )
    timeout_seconds: int = Field(
        default=900,
        ge=1,
        description="Default timeout in seconds for all subagents (default: 900 = 15 minutes)",
    )
    max_turns: int | None = Field(
        default=None,
        ge=1,
        description="Optional default max-turn override for all subagents (None = keep builtin defaults)",
    )
    agents: dict[str, SubagentOverrideConfig] = Field(
        default_factory=dict,
        description="Per-agent configuration overrides keyed by agent name",
    )
    custom_agents: dict[str, CustomSubagentConfig] = Field(
        default_factory=dict,
        description="User-defined subagent types keyed by agent name",
    )

    def get_timeout_for(self, agent_name: str) -> int:
        """Get the effective timeout for a specific agent.

        Args:
            agent_name: The name of the subagent.

        Returns:
            The timeout in seconds, using per-agent override if set, otherwise global default.
        """
        override = self.agents.get(agent_name)
        if override is not None and override.timeout_seconds is not None:
            return override.timeout_seconds
        return self.timeout_seconds

    def get_model_for(self, agent_name: str) -> str | None:
        """Get the model override for a specific agent.

        Args:
            agent_name: The name of the subagent.

        Returns:
            Model name if overridden, None otherwise (subagent will inherit parent model).
        """
        override = self.agents.get(agent_name)
        if override is not None and override.model is not None:
            return override.model
        return None

    def get_max_turns_for(self, agent_name: str, builtin_default: int) -> int:
        """Get the effective max_turns for a specific agent."""
        override = self.agents.get(agent_name)
        if override is not None and override.max_turns is not None:
            return override.max_turns
        if self.max_turns is not None:
            return self.max_turns
        return builtin_default

    def get_skills_for(self, agent_name: str) -> list[str] | None:
        """Get the skills override for a specific agent.

        Args:
            agent_name: The name of the subagent.

        Returns:
            Skill names whitelist if overridden, None otherwise (subagent will inherit all enabled skills).
        """
        override = self.agents.get(agent_name)
        if override is not None and override.skills is not None:
            return override.skills
        return None

    def get_model_settings_for(self, agent_name: str) -> ModelSettingsConfig | None:
        """Get the model_settings override for a specific agent.

        Args:
            agent_name: The name of the subagent.

        Returns:
            ModelSettingsConfig if overridden, None otherwise (subagent will use model defaults).
        """
        override = self.agents.get(agent_name)
        if override is not None and override.model_settings is not None:
            return override.model_settings
        return None

    def get_thinking_enabled_for(self, agent_name: str) -> bool | None:
        """Get the thinking_enabled override for a specific agent.

        Args:
            agent_name: The name of the subagent.

        Returns:
            True/False if overridden, None otherwise (subagent will inherit from parent agent).
        """
        override = self.agents.get(agent_name)
        if override is not None and override.thinking_enabled is not None:
            return override.thinking_enabled
        return None

    def get_reasoning_effort_for(self, agent_name: str) -> str | None:
        """Get the reasoning_effort override for a specific agent.

        Args:
            agent_name: The name of the subagent.

        Returns:
            Reasoning effort string if overridden, None otherwise (subagent will use model/factory default).
        """
        override = self.agents.get(agent_name)
        if override is not None and override.reasoning_effort is not None:
            return override.reasoning_effort
        return None


_subagents_config: SubagentsAppConfig = SubagentsAppConfig()


def get_subagents_app_config() -> SubagentsAppConfig:
    """Get the current subagents configuration."""
    return _subagents_config


def load_subagents_config_from_dict(config_dict: dict) -> None:
    """Load subagents configuration from a dictionary."""
    global _subagents_config
    _subagents_config = SubagentsAppConfig(**config_dict)

    overrides_summary = {}
    for name, override in _subagents_config.agents.items():
        parts = []
        if override.timeout_seconds is not None:
            parts.append(f"timeout={override.timeout_seconds}s")
        if override.max_turns is not None:
            parts.append(f"max_turns={override.max_turns}")
        if override.model is not None:
            parts.append(f"model={override.model}")
        if override.skills is not None:
            parts.append(f"skills={override.skills}")
        if override.model_settings is not None:
            parts.append(f"model_settings={override.model_settings.model_dump(exclude_none=True)}")
        if override.thinking_enabled is not None:
            parts.append(f"thinking_enabled={override.thinking_enabled}")
        if override.reasoning_effort is not None:
            parts.append(f"reasoning_effort={override.reasoning_effort}")
        if parts:
            overrides_summary[name] = ", ".join(parts)

    custom_agents_names = list(_subagents_config.custom_agents.keys())

    if overrides_summary or custom_agents_names:
        logger.info(
            "Subagents config loaded: default timeout=%ss, default max_turns=%s, per-agent overrides=%s, custom_agents=%s",
            _subagents_config.timeout_seconds,
            _subagents_config.max_turns,
            overrides_summary or "none",
            custom_agents_names or "none",
        )
    else:
        logger.info(
            "Subagents config loaded: default timeout=%ss, default max_turns=%s, no per-agent overrides",
            _subagents_config.timeout_seconds,
            _subagents_config.max_turns,
        )
