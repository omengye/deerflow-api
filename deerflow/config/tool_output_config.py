"""Configuration for tool output budget protection."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ToolOutputConfig(BaseModel):
    """Config section for tool-result output budget enforcement."""

    enabled: bool = Field(default=True, description="Enable the tool output budget middleware.")
    externalize_min_chars: int = Field(
        default=12_000,
        ge=0,
        description="Character threshold to persist oversized tool outputs.",
    )
    preview_head_chars: int = Field(default=2_000, ge=0, description="Head characters kept in persisted-output previews.")
    preview_tail_chars: int = Field(default=1_000, ge=0, description="Tail characters kept in persisted-output previews.")
    structured_synopsis_enabled: bool = Field(
        default=True,
        description="When persisting an oversized tool output, replace the head/tail preview with a deterministic structural synopsis (keys, types, lengths) if the content is JSON or JSON Lines.",
    )
    structured_synopsis_max_chars: int = Field(
        default=4_000,
        ge=0,
        description="Maximum characters for the structured synopsis inserted into persisted-output previews.",
    )
    fallback_max_chars: int = Field(default=30_000, ge=0, description="Maximum inline characters when persistence is unavailable.")
    fallback_head_chars: int = Field(default=8_000, ge=0, description="Head characters for inline fallback truncation.")
    fallback_tail_chars: int = Field(default=3_000, ge=0, description="Tail characters for inline fallback truncation.")
    storage_subdir: str = Field(default=".tool-results", description="Subdirectory under thread outputs for persisted tool results.")
    exempt_tools: list[str] = Field(
        default_factory=lambda: ["read_file", "read_file_tool"],
        description="Tool names exempt from budget enforcement.",
    )
    tool_overrides: dict[str, int] = Field(
        default_factory=dict,
        description="Per-tool externalize_min_chars overrides.",
    )
