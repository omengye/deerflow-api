"""Configuration objects for ``Harness``.

Users normally pass plain kwargs to ``Harness(...)`` and never touch these
classes. They exist so that:

  * Configuration can be loaded from a file (yaml/json) by an adapter.
  * IDEs / mypy can validate the shape of programmatic config.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelConfig(BaseModel):
    """LLM model configuration.

    A bare string passed to ``Harness(model="qwen3.6-plus")`` is
    auto-converted to ``ModelConfig(name="qwen3.6-plus")``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Model identifier, e.g. 'qwen3.6-plus'.")
    provider: str | None = Field(
        default=None,
        description="Provider hint ('dashscope', 'openai', 'anthropic', ...). "
        "If omitted, inferred from the model name.",
    )
    temperature: float | None = None
    max_tokens: int | None = None
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-specific kwargs forwarded as-is.",
    )


class HarnessConfig(BaseModel):
    """Programmatic equivalent of the v0.1 ``Harness(...)`` kwargs.

    Used by the legacy yaml loader (``deerflow_legacy.config_loader``)
    and by users who prefer config objects over keyword arguments.

    .. note::
       Tool/subagent/hook/permission/sandbox instances are **not** in this
       config (they are Python objects, not data). Pass them directly to
       ``Harness(...)``.
    """

    model_config = ConfigDict(extra="forbid")

    model: str | ModelConfig
    system_prompt: str | None = None
    max_iterations: int = Field(default=50, ge=1, le=1000)
