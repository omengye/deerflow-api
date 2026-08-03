"""Subagent configuration definitions."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SubagentConfig:
    """Configuration for a subagent.

    Attributes:
        name: Unique identifier for the subagent.
        description: When Claude should delegate to this subagent.
        system_prompt: The system prompt that guides the subagent's behavior.
        tools: Optional list of tool names to allow. If None, inherits all tools.
        disallowed_tools: Optional list of tool names to deny.
        skills: Optional list of skill names to make discoverable. If None,
                inherits all enabled skills. If empty, skills are disabled.
                Skill bodies are read lazily by the subagent when relevant.
        model: Model to use - 'inherit' uses parent's model.
        max_turns: Maximum number of agent turns before stopping.
        timeout_seconds: Maximum execution time in seconds (default: 900 = 15 minutes).
        model_settings: Generation parameter overrides (temperature, max_tokens) passed
                        through to create_chat_model as kwargs. None = model defaults.
                        Already a plain dict (dumped from ModelSettingsConfig) so this
                        module stays free of the pydantic config schema.
        thinking_enabled: Explicit thinking mode override. None = inherit from parent agent.
        reasoning_effort: Explicit reasoning_effort override. None = model/factory default.
    """

    name: str
    description: str
    system_prompt: str
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = field(default_factory=lambda: ["task"])
    skills: list[str] | None = None
    model: str = "inherit"
    max_turns: int = 50
    timeout_seconds: int = 900
    model_settings: dict[str, Any] | None = None
    thinking_enabled: bool | None = None
    reasoning_effort: str | None = None
