"""Cache contract and collision-safe keys for delta histories."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

CACHE_FORMAT_VERSION = 1


def _thread_component(thread_id: str) -> str:
    """Hide raw IDs and make per-thread key stems prefix/glob safe."""
    return hashlib.sha256(thread_id.encode()).hexdigest()[:24]


def make_history_key(
    key_prefix: str,
    thread_id: str,
    checkpoint_ns: str,
    checkpoint_id: str,
    channel: str,
) -> str:
    digest = hashlib.sha256(
        f"{checkpoint_ns}\x00{checkpoint_id}\x00{channel}".encode()
    ).hexdigest()[:24]
    return f"{key_prefix}:{_thread_component(thread_id)}:{digest}"


def thread_key_stem(key_prefix: str, thread_id: str) -> str:
    return f"{key_prefix}:{_thread_component(thread_id)}:"


def redis_glob_escape(value: str) -> str:
    """Escape a literal Redis key prefix for use in a SCAN MATCH pattern."""
    return "".join(f"\\{char}" if char in "\\*?[]" else char for char in value)


@dataclass
class CheckpointCacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    entries: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "entries": self.entries,
        }


def copy_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {"writes": list(entry.get("writes", []))}
    if "seed" in entry:
        copied["seed"] = entry["seed"]
    return copied
