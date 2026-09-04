from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

from deerflow.config.extensions_config import ExtensionsConfig, McpOAuthConfig, McpServerConfig
from deerflow.mcp.client import build_server_params, build_servers_config
from deerflow.mcp.headers import illegal_header_value_reason
from deerflow.mcp.oauth import OAuthTokenManager, _OAuthToken


@pytest.mark.parametrize(
    "value",
    [
        "Bearer token",
        "Bearer  token",
        "Bearer\ttoken",
        "value\x7f",
        "",
    ],
)
def test_header_value_validator_accepts_transportable_ascii(value: str) -> None:
    assert illegal_header_value_reason(value) is None


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("Bearer secret\n", "line break"),
        ("Bearer\rsecret", "line break"),
        ("Bearer secret ", "whitespace"),
        ("\tBearer secret", "whitespace"),
        ("Bearer secrét", "outside ASCII"),
    ],
)
def test_header_value_validator_rejects_transport_errors(value: str, reason: str) -> None:
    assert reason in (illegal_header_value_reason(value) or "")


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("Bearer static-secret\n", "line break"),
        ("Bearer static-secret ", "whitespace"),
        ("Bearer static-secrét", "outside ASCII"),
    ],
)
def test_static_mcp_header_is_rejected_without_leaking_value(value: str, reason: str) -> None:
    config = McpServerConfig(
        type="http",
        url="https://example.invalid/mcp",
        headers={"Authorization": value},
    )

    with pytest.raises(ValueError) as exc_info:
        build_server_params("remote", config)

    message = str(exc_info.value)
    assert reason in message
    assert "static-secret" not in message
    assert "static-secrét" not in message
    assert "Authorization" in message


def test_invalid_static_header_drops_only_affected_server_without_log_leak(caplog: pytest.LogCaptureFixture) -> None:
    config = ExtensionsConfig.model_validate(
        {
            "mcpServers": {
                "broken": {
                    "type": "http",
                    "url": "https://example.invalid/mcp",
                    "headers": {"Authorization": "Bearer leaked-secret\n"},
                },
                "healthy": {
                    "type": "http",
                    "url": "https://example.invalid/mcp",
                    "headers": {"Authorization": "Bearer valid"},
                },
            }
        }
    )

    with caplog.at_level(logging.ERROR, logger="deerflow.mcp.client"):
        result = build_servers_config(config)

    assert set(result) == {"healthy"}
    assert "leaked-secret" not in caplog.text


def _oauth_manager() -> OAuthTokenManager:
    oauth = McpOAuthConfig(
        token_url="https://example.invalid/token",
        client_id="client",
        client_secret="secret",
        refresh_skew_seconds=0,
    )
    return OAuthTokenManager({"remote": oauth})


@pytest.mark.parametrize(
    "token",
    [
        _OAuthToken("oauth-secret\n", "Bearer", datetime.now(UTC) + timedelta(hours=1)),
        _OAuthToken("oauth-secrét", "Bearer", datetime.now(UTC) + timedelta(hours=1)),
        _OAuthToken("oauth-secret", "Bearer\r", datetime.now(UTC) + timedelta(hours=1)),
        _OAuthToken("oauth-secret ", "Bearer", datetime.now(UTC) + timedelta(hours=1)),
    ],
)
def test_oauth_header_is_rejected_without_leaking_token(token: _OAuthToken) -> None:
    manager = _oauth_manager()
    manager._store_token("remote", token)

    with pytest.raises(ValueError) as exc_info:
        asyncio.run(manager.get_authorization_header("remote"))

    message = str(exc_info.value)
    assert "oauth-secret" not in message
    assert "oauth-secrét" not in message
    assert "remote" in message


def test_oauth_header_validates_the_rendered_value() -> None:
    manager = _oauth_manager()
    manager._store_token(
        "remote",
        _OAuthToken(
            " leading-space-token",
            "Bearer",
            datetime.now(UTC) + timedelta(hours=1),
        ),
    )

    assert asyncio.run(manager.get_authorization_header("remote")) == "Bearer  leading-space-token"
