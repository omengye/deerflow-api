"""Provider-neutral finish-reason detectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from langchain_core.messages import AIMessage


@dataclass(frozen=True)
class FinishReasonTermination:
    detector: str
    reason_field: str
    reason_value: str
    extras: dict[str, Any] = field(default_factory=dict)


class FinishReasonDetector(Protocol):
    name: str

    def detect(self, message: AIMessage) -> FinishReasonTermination | None: ...


def _metadata_value(message: AIMessage, field_name: str) -> str | None:
    for container_name in ("response_metadata", "additional_kwargs"):
        container = getattr(message, container_name, None) or {}
        if isinstance(container, dict):
            value = container.get(field_name)
            if isinstance(value, str) and value:
                return value
    return None


class OpenAILengthDetector:
    name = "openai_compatible_length"

    def detect(self, message: AIMessage) -> FinishReasonTermination | None:
        value = _metadata_value(message, "finish_reason")
        if value is None or value.casefold() not in {"length", "max_tokens"}:
            return None
        return FinishReasonTermination(self.name, "finish_reason", value)


class AnthropicLengthDetector:
    name = "anthropic_max_tokens"

    def detect(self, message: AIMessage) -> FinishReasonTermination | None:
        value = _metadata_value(message, "stop_reason")
        if value is None or value.casefold() != "max_tokens":
            return None
        return FinishReasonTermination(self.name, "stop_reason", value)


class GeminiLengthDetector:
    name = "gemini_max_tokens"

    def detect(self, message: AIMessage) -> FinishReasonTermination | None:
        value = _metadata_value(message, "finish_reason")
        if value is None or value.upper() != "MAX_TOKENS":
            return None
        return FinishReasonTermination(self.name, "finish_reason", value)


class OpenAIContentFilterDetector:
    name = "openai_compatible_content_filter"

    def detect(self, message: AIMessage) -> FinishReasonTermination | None:
        value = _metadata_value(message, "finish_reason")
        if value is None or value.casefold() not in {"content_filter", "sensitive", "violation"}:
            return None
        response_metadata = getattr(message, "response_metadata", None) or {}
        extras = {}
        if isinstance(response_metadata, dict) and response_metadata.get("content_filter_results"):
            extras["content_filter_results"] = response_metadata["content_filter_results"]
        return FinishReasonTermination(self.name, "finish_reason", value, extras)


class AnthropicRefusalDetector:
    name = "anthropic_refusal"

    def detect(self, message: AIMessage) -> FinishReasonTermination | None:
        value = _metadata_value(message, "stop_reason")
        if value is None or value.casefold() != "refusal":
            return None
        return FinishReasonTermination(self.name, "stop_reason", value)


class GeminiSafetyDetector:
    name = "gemini_safety"
    _REASONS = {
        "SAFETY",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "RECITATION",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
    }

    def detect(self, message: AIMessage) -> FinishReasonTermination | None:
        value = _metadata_value(message, "finish_reason")
        if value is None or value.upper() not in self._REASONS:
            return None
        response_metadata = getattr(message, "response_metadata", None) or {}
        extras = {}
        if isinstance(response_metadata, dict) and response_metadata.get("safety_ratings"):
            extras["safety_ratings"] = response_metadata["safety_ratings"]
        return FinishReasonTermination(self.name, "finish_reason", value, extras)


def length_detectors() -> list[FinishReasonDetector]:
    return [OpenAILengthDetector(), AnthropicLengthDetector(), GeminiLengthDetector()]


def safety_detectors() -> list[FinishReasonDetector]:
    return [OpenAIContentFilterDetector(), AnthropicRefusalDetector(), GeminiSafetyDetector()]
