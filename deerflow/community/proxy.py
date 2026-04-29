"""Proxy configuration helpers for community network tools."""

from deerflow.config import get_app_config

DEFAULT_HTTPS_PROXY = "http://172.17.0.1:11055"

_SENTINEL = object()


def get_tool_https_proxy(tool_name: str) -> str | None:
    """Return the configured HTTPS proxy for a network tool.

    Tools default to the host proxy used by local Docker-style deployments.
    Set ``https_proxy`` to an empty string or null in config.yaml to disable
    proxying for a specific tool.
    """
    try:
        tool_config = get_app_config().get_tool_config(tool_name)
    except Exception:
        return DEFAULT_HTTPS_PROXY

    if tool_config is None:
        return DEFAULT_HTTPS_PROXY

    extras = tool_config.model_extra or {}
    value = extras.get("https_proxy", _SENTINEL)
    if value is _SENTINEL:
        # No override at all — fall back to the default host proxy.
        return DEFAULT_HTTPS_PROXY
    if value is None:
        # Explicit ``null`` in config: caller wants the proxy disabled.
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)
