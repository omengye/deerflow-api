"""Regression tests for the configured default-model contract."""

import logging
from unittest.mock import patch

import pytest

from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.models import factory as factory_module
from deerflow.tools import tools as tools_module
from deerflow.tools.builtins import view_image_tool


def _model(name: str, *, supports_vision: bool = False) -> ModelConfig:
    return ModelConfig(
        name=name,
        use="tests.fake:FakeChatModel",
        model=f"provider-{name}",
        supports_vision=supports_vision,
    )


def _app_config(*, default_model: str | None = "second-model") -> AppConfig:
    return AppConfig(
        sandbox=SandboxConfig(use="test"),
        models=[_model("first-model"), _model("second-model")],
        default_model=default_model,
    )


def test_app_config_resolves_explicit_default_model_instead_of_first_model() -> None:
    config = _app_config()

    assert config.get_default_model_name() == "second-model"


def test_app_config_falls_back_to_first_model_when_default_is_omitted() -> None:
    config = _app_config(default_model=None)

    assert config.get_default_model_name() == "first-model"


def test_app_config_warns_and_falls_back_when_default_is_invalid(caplog: pytest.LogCaptureFixture) -> None:
    config = _app_config(default_model="missing-model")

    with caplog.at_level(logging.WARNING):
        resolved = config.get_default_model_name()

    assert resolved == "first-model"
    assert any("missing-model" in record.message and "first-model" in record.message for record in caplog.records)


def test_app_config_rejects_default_resolution_without_models() -> None:
    config = AppConfig(sandbox=SandboxConfig(use="test"), models=[])

    with pytest.raises(ValueError, match="No chat models are configured"):
        config.get_default_model_name()


def test_model_factory_uses_configured_default_model() -> None:
    config = _app_config()

    class FakeChatModel:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.callbacks = []

    with (
        patch.object(factory_module, "get_app_config", return_value=config),
        patch.object(factory_module, "resolve_class", return_value=FakeChatModel),
        patch.object(factory_module, "build_tracing_callbacks", return_value=[]),
    ):
        model = factory_module.create_chat_model()

    assert model.kwargs["model"] == "provider-second-model"


def test_model_factory_explicit_name_overrides_configured_default() -> None:
    config = _app_config()

    class FakeChatModel:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.callbacks = []

    with (
        patch.object(factory_module, "get_app_config", return_value=config),
        patch.object(factory_module, "resolve_class", return_value=FakeChatModel),
        patch.object(factory_module, "build_tracing_callbacks", return_value=[]),
    ):
        model = factory_module.create_chat_model(name="first-model")

    assert model.kwargs["model"] == "provider-first-model"


def test_lead_agent_resolver_uses_configured_default_model() -> None:
    from deerflow.agents.lead_agent import agent as lead_agent

    config = _app_config()

    with patch.object(lead_agent, "get_app_config", return_value=config):
        assert lead_agent._resolve_model_name() == "second-model"
        assert lead_agent._resolve_model_name("first-model") == "first-model"


def test_tool_capabilities_use_configured_default_model() -> None:
    config = AppConfig(
        sandbox=SandboxConfig(use="test"),
        models=[_model("first-model"), _model("second-model", supports_vision=True)],
        default_model="second-model",
    )

    with patch.object(tools_module, "get_app_config", return_value=config):
        available = tools_module.get_available_tools(include_mcp=False)

    assert view_image_tool in available
