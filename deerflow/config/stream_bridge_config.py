"""Configuration for stream bridge."""

from typing import Literal

from pydantic import BaseModel, Field

StreamBridgeType = Literal["memory", "redis"]


class StreamBridgeConfig(BaseModel):
    """Configuration for the stream bridge that connects agent workers to SSE endpoints."""

    type: StreamBridgeType = Field(
        default="memory",
        description="Stream bridge backend type. 'memory' uses an in-process buffer (single-process only). 'redis' uses Redis Streams.",
    )
    redis_url: str | None = Field(
        default=None,
        description="Redis URL for the redis stream bridge type. Example: 'redis://localhost:6379/0'.",
    )
    redis_key_prefix: str = Field(
        default="deerflow:stream",
        description="Redis key prefix for per-run stream keys.",
    )
    redis_maxlen: int = Field(
        default=10000,
        ge=1,
        description="Approximate maximum Redis Stream entries retained per run.",
    )
    redis_retention_seconds: int = Field(
        default=3600,
        ge=0,
        description="Redis safety TTL refreshed while events are being written. Lifecycle retention is controlled by the replay settings below.",
    )
    reconnect_grace_seconds: int = Field(
        default=600,
        ge=0,
        description="Seconds to keep buffering events after the last client disconnects from an active run.",
    )
    completed_replay_seconds: int = Field(
        default=1800,
        ge=0,
        description="Seconds to retain a completed run's event stream for replay.",
    )
    run_metadata_retention_seconds: int = Field(
        default=3600,
        ge=0,
        description="Seconds to retain run metadata after completion. Values shorter than completed_replay_seconds are extended at runtime.",
    )
    queue_maxsize: int = Field(
        default=256,
        description="Maximum number of events buffered per run in the memory bridge.",
    )


# Global configuration instance — None means no stream bridge is configured
# (falls back to memory with defaults).
_stream_bridge_config: StreamBridgeConfig | None = None


def get_stream_bridge_config() -> StreamBridgeConfig | None:
    """Get the current stream bridge configuration, or None if not configured."""
    return _stream_bridge_config


def set_stream_bridge_config(config: StreamBridgeConfig | None) -> None:
    """Set the stream bridge configuration."""
    global _stream_bridge_config
    _stream_bridge_config = config


def load_stream_bridge_config_from_dict(config_dict: dict) -> None:
    """Load stream bridge configuration from a dictionary."""
    global _stream_bridge_config
    _stream_bridge_config = StreamBridgeConfig(**config_dict)
