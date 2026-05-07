"""Web search tool using Brave Search API."""

import json
import logging

import httpx
from langchain.tools import tool

from deerflow.community.proxy import get_tool_https_proxy
from deerflow.config import get_app_config

logger = logging.getLogger(__name__)

_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_DEFAULT_SEARCH_TIMEOUT = 30
_MAX_SEARCH_TIMEOUT = 120
_MAX_RESULTS_CAP = 20


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


def _get_api_key(tool_name: str) -> str | None:
    """Read api_key from tool config extra fields."""
    try:
        config = get_app_config().get_tool_config(tool_name)
    except Exception:
        return None
    if config is None or not config.model_extra:
        return None
    return config.model_extra.get("api_key") or None


def _search_text(
    query: str,
    api_key: str,
    max_results: int = 5,
    country: str = "us",
    search_lang: str = "en",
    safesearch: str = "moderate",
    https_proxy: str | None = None,
    timeout: int = _DEFAULT_SEARCH_TIMEOUT,
) -> list[dict]:
    """Execute text search using Brave Search API."""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {
        "q": query,
        "count": max_results,
        "country": country,
        "search_lang": search_lang,
        "safesearch": safesearch,
        "text_decorations": False,
    }

    try:
        with httpx.Client(
            timeout=timeout,
            proxy=https_proxy or None,
            trust_env=False,
        ) as client:
            response = client.get(_BRAVE_SEARCH_URL, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        logger.error("Brave Search API error: %s %s", e.response.status_code, type(e).__name__)
        return []
    except Exception as e:
        logger.error("Failed to search web via Brave: %s", type(e).__name__)
        return []

    web_results = data.get("web", {}).get("results", [])
    return web_results


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

    if config is not None and config.model_extra:
        if "max_results" in config.model_extra:
            max_results = config.model_extra.get("max_results", max_results)

    max_results = _clamp_max_results(max_results)

    api_key = _get_api_key("web_search")
    if not api_key:
        return json.dumps(
            {"error": "Brave Search API key not configured. Set api_key in config.yaml under the web_search tool.", "query": query},
            ensure_ascii=False,
        )

    results = _search_text(
        query=query,
        api_key=api_key,
        max_results=max_results,
        https_proxy=get_tool_https_proxy("web_search"),
        timeout=_resolve_search_timeout("web_search"),
    )

    if not results:
        return json.dumps({"error": "No results found", "query": query}, ensure_ascii=False)

    normalized_results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("description", ""),
        }
        for r in results
    ]

    output = {
        "query": query,
        "total_results": len(normalized_results),
        "results": normalized_results,
    }

    return json.dumps(output, indent=2, ensure_ascii=False)
