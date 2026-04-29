"""Web search tool using DuckDuckGo via ddgs."""

import json
import logging

from langchain.tools import tool

from deerflow.community.proxy import get_tool_https_proxy
from deerflow.config import get_app_config

logger = logging.getLogger(__name__)

_DEFAULT_SEARCH_TIMEOUT = 30
_MAX_SEARCH_TIMEOUT = 120
_MAX_RESULTS_CAP = 50


def _resolve_search_timeout(tool_name: str) -> int:
    """Resolve timeout (seconds) for a search tool from config with clamping."""
    try:
        config = get_app_config().get_tool_config(tool_name)
    except Exception:
        return _DEFAULT_SEARCH_TIMEOUT
    if config is None or not config.model_extra:
        return _DEFAULT_SEARCH_TIMEOUT
    raw = config.model_extra.get("timeout")
    if raw is None:
        return _DEFAULT_SEARCH_TIMEOUT
    try:
        return max(1, min(int(raw), _MAX_SEARCH_TIMEOUT))
    except (TypeError, ValueError):
        return _DEFAULT_SEARCH_TIMEOUT


def _clamp_max_results(value: int, default: int = 5) -> int:
    try:
        return max(1, min(int(value), _MAX_RESULTS_CAP))
    except (TypeError, ValueError):
        return default


def _search_text(
    query: str,
    max_results: int = 5,
    region: str = "wt-wt",
    safesearch: str = "moderate",
    https_proxy: str | None = None,
    timeout: int = _DEFAULT_SEARCH_TIMEOUT,
) -> list[dict]:
    """Execute text search using DuckDuckGo."""
    try:
        from ddgs import DDGS
    except ImportError:
        logger.error("ddgs library not installed. Run: pip install ddgs")
        return []

    ddgs = DDGS(proxy=https_proxy, timeout=timeout)

    try:
        results = ddgs.text(
            query,
            region=region,
            safesearch=safesearch,
            max_results=max_results,
        )
        return list(results) if results else []
    except Exception as e:
        # Avoid leaking proxy URL or query payload in logs.
        logger.error("Failed to search web: %s", type(e).__name__)
        return []


@tool("web_search", parse_docstring=True)
def web_search_tool(
    query: str,
    max_results: int = 5,
) -> str:
    """Search the web for information. Use this tool to find current information, news, articles, and facts from the internet.

    Args:
        query: Search keywords describing what you want to find. Be specific for better results.
        max_results: Maximum number of results to return. Default is 5.
    """
    config = get_app_config().get_tool_config("web_search")

    if config is not None and config.model_extra and "max_results" in config.model_extra:
        max_results = config.model_extra.get("max_results", max_results)

    max_results = _clamp_max_results(max_results)

    results = _search_text(
        query=query,
        max_results=max_results,
        https_proxy=get_tool_https_proxy("web_search"),
        timeout=_resolve_search_timeout("web_search"),
    )

    if not results:
        return json.dumps({"error": "No results found", "query": query}, ensure_ascii=False)

    normalized_results = [
        {
            "title": r.get("title", ""),
            "url": r.get("href", r.get("link", "")),
            "content": r.get("body", r.get("snippet", "")),
        }
        for r in results
    ]

    output = {
        "query": query,
        "total_results": len(normalized_results),
        "results": normalized_results,
    }

    return json.dumps(output, indent=2, ensure_ascii=False)
