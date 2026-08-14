import json
from pathlib import Path

import yaml

from deerflow.config.extensions_config import ExtensionsConfig
from deerflow.config.guardrails_config import GuardrailsConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_openviking_example_is_disabled_and_resolves_api_key(
    monkeypatch,
) -> None:
    path = PROJECT_ROOT / "extensions_config.example.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["mcpServers"]["openviking"]["enabled"] is False
    assert raw["mcpServers"]["openviking"]["headers"] == {
        "X-API-Key": "$OPENVIKING_API_KEY"
    }

    monkeypatch.setenv("OPENVIKING_API_KEY", "owner-bound-user-key")
    config = ExtensionsConfig.from_file(str(path))
    server = config.mcp_servers["openviking"]
    assert server.headers == {"X-API-Key": "owner-bound-user-key"}
    assert server.tool_name_prefix is True


def test_guardrail_example_uses_runtime_schema_and_denies_forget() -> None:
    raw = yaml.safe_load((PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    guardrails = GuardrailsConfig.model_validate(raw["guardrails"])

    assert guardrails.fail_closed is True
    assert guardrails.provider is not None
    assert guardrails.provider.config["denied_tools"] == ["openviking_forget"]
