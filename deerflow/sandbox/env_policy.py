"""Environment policy for commands that execute inside a sandbox boundary."""

from __future__ import annotations

import os
from collections.abc import Mapping


_BLOCKED_EXACT_NAMES = frozenset(
    {
        "GIT_ASKPASS",
        "SSH_AGENT_PID",
        "SSH_ASKPASS",
        "SSH_ASKPASS_REQUIRE",
        "SSH_AUTH_SOCK",
    }
)


def build_sandbox_subprocess_env(
    inherited: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy host environment while removing ambient credential capabilities.

    Explicit sandbox/request-scoped overrides are applied after the blocklist,
    allowing a caller to grant a credential intentionally without inheriting
    the host's ambient value.
    """
    source = os.environ if inherited is None else inherited
    environment = {
        name: value
        for name, value in source.items()
        if name.upper() not in _BLOCKED_EXACT_NAMES
    }
    if overrides:
        environment.update(overrides)
    return environment
