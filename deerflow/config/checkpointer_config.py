"""Configuration for LangGraph checkpointer and delta-history caching."""

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

CheckpointerType = Literal["memory", "sqlite", "postgres"]
CheckpointChannelMode = Literal["full", "delta"]
CheckpointCacheType = Literal["memory", "redis"]

DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY = 10


class CheckpointDeltaConfig(BaseModel):
    """Restart-required tuning for the ``DeltaChannel`` representation."""

    snapshot_frequency: int = Field(
        default=DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY,
        ge=1,
        description=(
            "Write a full messages snapshot every N delta updates. Higher "
            "values reduce storage but increase cold materialization latency."
        ),
    )


class CheckpointCacheConfig(BaseModel):
    """Performance-only cache for immutable delta-channel histories."""

    type: CheckpointCacheType = "memory"
    max_entries: int = Field(default=128, ge=0)
    redis_url: str | None = None
    ttl_seconds: int = Field(default=86400, ge=0)
    key_prefix: str = ""


class CheckpointerConfig(BaseModel):
    """Configuration for LangGraph state persistence checkpointer."""

    type: CheckpointerType = Field(
        description="Checkpointer backend type. "
        "'memory' is in-process only (lost on restart). "
        "'sqlite' persists to a local file (requires langgraph-checkpoint-sqlite). "
        "'postgres' persists to PostgreSQL (requires langgraph-checkpoint-postgres)."
    )
    connection_string: str | None = Field(
        default=None,
        description="Connection string for sqlite (file path) or postgres (DSN). "
        "Required for sqlite and postgres types. "
        "For sqlite, use a file path like '.deer-flow/checkpoints.db' or ':memory:' for in-memory. "
        "For postgres, use a DSN like 'postgresql://user:pass@localhost:5432/db'.",
    )
    channel_mode: CheckpointChannelMode = Field(
        default="full",
        description=(
            "Checkpoint representation. 'delta' stores message writes through "
            "LangGraph DeltaChannel. Restart required; every process sharing "
            "the database must use the same value."
        ),
    )
    delta: CheckpointDeltaConfig = Field(default_factory=CheckpointDeltaConfig)
    cache: CheckpointCacheConfig = Field(default_factory=CheckpointCacheConfig)

    @model_validator(mode="before")
    @classmethod
    def _migrate_flat_delta_frequency(cls, data: Any) -> Any:
        """Carry the short-lived flat cadence key into ``delta`` safely."""
        if not isinstance(data, dict) or "delta_snapshot_frequency" not in data:
            return data
        migrated = dict(data)
        value = migrated.pop("delta_snapshot_frequency")
        nested = migrated.get("delta")
        if isinstance(nested, dict) and "snapshot_frequency" in nested:
            logger.warning(
                "Both checkpointer.delta_snapshot_frequency and "
                "checkpointer.delta.snapshot_frequency are set; the nested value wins."
            )
            return migrated
        migrated["delta"] = {**(nested or {}), "snapshot_frequency": value}
        logger.warning(
            "checkpointer.delta_snapshot_frequency is deprecated; use "
            "checkpointer.delta.snapshot_frequency."
        )
        return migrated


# Global configuration instance — None means no checkpointer is configured.
_checkpointer_config: CheckpointerConfig | None = None


def get_checkpointer_config() -> CheckpointerConfig | None:
    """Get the current checkpointer configuration, or None if not configured."""
    return _checkpointer_config


def set_checkpointer_config(config: CheckpointerConfig | None) -> None:
    """Set the checkpointer configuration."""
    global _checkpointer_config
    _checkpointer_config = config


def load_checkpointer_config_from_dict(config_dict: dict) -> None:
    """Load checkpointer configuration from a dictionary."""
    global _checkpointer_config
    _checkpointer_config = CheckpointerConfig(**config_dict)
