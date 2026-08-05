"""Process-frozen checkpoint mode and fail-closed compatibility markers."""

from __future__ import annotations

from typing import Any

from deerflow.config.checkpointer_config import (
    DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY,
    CheckpointChannelMode,
)

INTERNAL_CHECKPOINT_MODE_KEY = "__deerflow_checkpoint_channel_mode"
CHECKPOINT_MODE_METADATA_KEY = "deerflow_checkpoint_channel_mode"


class CheckpointModeMismatchError(RuntimeError):
    """A full-mode process attempted to use a delta-mode thread."""


class CheckpointModeReconfigurationError(RuntimeError):
    """A restart-required checkpoint setting changed in-process."""


_frozen_mode: CheckpointChannelMode | None = None
_frozen_snapshot_frequency: int | None = None


def frozen_checkpoint_channel_mode() -> CheckpointChannelMode | None:
    return _frozen_mode


def frozen_checkpoint_snapshot_frequency() -> int | None:
    return _frozen_snapshot_frequency


def freeze_checkpoint_channel_mode(mode: CheckpointChannelMode) -> CheckpointChannelMode:
    global _frozen_mode
    if _frozen_mode is None:
        _frozen_mode = mode
    elif _frozen_mode != mode:
        raise CheckpointModeReconfigurationError(
            "checkpoint channel mode is restart-required and cannot change in a running process"
        )
    return _frozen_mode


def freeze_checkpoint_snapshot_frequency(snapshot_frequency: int) -> int:
    global _frozen_snapshot_frequency
    if snapshot_frequency <= 0:
        raise ValueError("checkpoint delta snapshot frequency must be positive")
    if _frozen_snapshot_frequency is None:
        _frozen_snapshot_frequency = snapshot_frequency
    elif _frozen_snapshot_frequency != snapshot_frequency:
        raise CheckpointModeReconfigurationError(
            "checkpoint delta snapshot frequency is restart-required and cannot change in a running process"
        )
    return _frozen_snapshot_frequency


def resolve_checkpoint_snapshot_frequency(value: int | None = None) -> int:
    if value is not None:
        return value
    return _frozen_snapshot_frequency or DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY


def inject_checkpoint_mode(config: dict[str, Any], mode: CheckpointChannelMode) -> None:
    configurable = config.setdefault("configurable", {})
    configurable[INTERNAL_CHECKPOINT_MODE_KEY] = mode
    metadata = config.setdefault("metadata", {})
    if mode == "delta":
        metadata[CHECKPOINT_MODE_METADATA_KEY] = "delta"
    else:
        metadata.pop(CHECKPOINT_MODE_METADATA_KEY, None)


def checkpoint_metadata_uses_delta(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    if metadata.get(CHECKPOINT_MODE_METADATA_KEY) == "delta":
        return True
    counters = metadata.get("counters_since_delta_snapshot")
    return isinstance(counters, dict) and "messages" in counters


def checkpoint_tuple_uses_delta(checkpoint_tuple: Any) -> bool:
    return checkpoint_tuple is not None and checkpoint_metadata_uses_delta(
        getattr(checkpoint_tuple, "metadata", {}) or {}
    )


def state_snapshot_uses_delta(snapshot: Any) -> bool:
    """Return whether a materialized LangGraph state came from delta storage."""
    return snapshot is not None and checkpoint_metadata_uses_delta(
        getattr(snapshot, "metadata", {}) or {}
    )


def raise_if_snapshot_incompatible(
    snapshot: Any,
    mode: CheckpointChannelMode,
) -> None:
    """Fail closed before a full-mode caller consumes partial delta state."""
    if mode == "full" and state_snapshot_uses_delta(snapshot):
        raise CheckpointModeMismatchError(
            "Thread requires delta checkpoint mode; materialize and convert its "
            "checkpoints before using full mode."
        )


def ensure_checkpoint_mode_compatible(
    checkpointer: Any,
    config: dict[str, Any],
    mode: CheckpointChannelMode,
) -> None:
    """Prevent a full graph from writing over a delta thread."""
    if mode == "delta" or checkpointer is None:
        return
    if checkpoint_tuple_uses_delta(checkpointer.get_tuple(config)):
        raise CheckpointModeMismatchError(
            "Thread requires delta checkpoint mode; restart with checkpoint_channel_mode=delta."
        )


async def aensure_checkpoint_mode_compatible(
    checkpointer: Any,
    config: dict[str, Any],
    mode: CheckpointChannelMode,
) -> None:
    if mode == "delta" or checkpointer is None:
        return
    if checkpoint_tuple_uses_delta(await checkpointer.aget_tuple(config)):
        raise CheckpointModeMismatchError(
            "Thread requires delta checkpoint mode; restart with checkpoint_channel_mode=delta."
        )


def reset_checkpoint_mode_for_tests() -> None:
    """Reset process-frozen values. Intended only for isolated tests."""
    global _frozen_mode, _frozen_snapshot_frequency
    _frozen_mode = None
    _frozen_snapshot_frequency = None
