"""Validated configuration for the hosted or self-hosted mem0 API."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Mem0FailurePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    read: Literal["fail_open", "fail_closed"] = "fail_open"
    write: Literal["log_and_drop", "raise"] = "log_and_drop"


class Mem0Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key_env: str = "MEM0_API_KEY"
    base_url: str = "https://api.mem0.ai"
    allow_insecure_http: bool = False
    default_user_id: str = Field(default="deerflow", min_length=1, max_length=256)
    top_k: int = Field(default=8, ge=1, le=100)
    score_threshold: float = Field(default=0.1, ge=0.0, le=1.0)
    max_injection_chars: int = Field(default=12000, ge=256, le=100000)
    timeout_seconds: float = Field(default=10.0, gt=0.0, le=120.0)
    startup_policy: Literal["fail_fast", "best_effort"] = "fail_fast"
    failure_policy: Mem0FailurePolicy = Field(default_factory=Mem0FailurePolicy)

    @field_validator("api_key_env")
    @classmethod
    def _validate_env_name(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("api_key_env must be an environment-variable name")
        return value

    @model_validator(mode="after")
    def _validate_transport(self) -> Mem0Config:
        self.base_url = self.base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.scheme != "https" and not self.allow_insecure_http:
            raise ValueError(
                "mem0 base_url must use HTTPS unless allow_insecure_http=true"
            )
        return self
