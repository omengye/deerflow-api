import asyncio
import concurrent.futures
import json
import logging

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from deerflow.community.jina_ai.jina_client import JinaClient
from deerflow.community.proxy import get_tool_https_proxy
from deerflow.config import get_app_config
from deerflow.utils.readability import ReadabilityExtractor

logger = logging.getLogger(__name__)

readability_extractor = ReadabilityExtractor()

_WEB_FETCH_DESCRIPTION = """Fetch the contents of a web page at a given URL.
Only fetch EXACT URLs that have been provided directly by the user or have been returned in results from the web_search and web_fetch tools.
This tool can NOT access content that requires authentication, such as private Google Docs or pages behind login walls.
Do NOT add www. to URLs that do NOT have them.
URLs must include the schema: https://example.com is a valid URL while example.com is an invalid URL."""


class _WebFetchInput(BaseModel):
    url: str = Field(description="The URL to fetch the contents of.")


async def _fetch_direct(url: str, timeout: int, https_proxy: str | None = None) -> str:
    headers = {
        "User-Agent": "DeerFlow/1.0 (+https://github.com/bytedance/deer-flow)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers, proxy=https_proxy, trust_env=False) as client:
            response = await client.get(url)
        if response.status_code >= 400:
            # Truncate body to avoid spilling large payloads back to the LLM.
            preview = (response.text or "")[:500]
            return f"Error: Direct fetch returned status {response.status_code}: {preview}"
        return response.text
    except Exception as e:
        return f"Error: Direct fetch failed: {type(e).__name__}"


_DEFAULT_FETCH_TIMEOUT = 10
_MAX_FETCH_TIMEOUT = 120
_MAX_OUTPUT_CHARS = 4096


def _resolve_fetch_timeout() -> int:
    """Resolve the web_fetch timeout from config with safe clamping."""
    timeout = _DEFAULT_FETCH_TIMEOUT
    try:
        config = get_app_config().get_tool_config("web_fetch")
    except Exception:
        return timeout
    if config is None:
        return timeout
    raw = config.model_extra.get("timeout") if config.model_extra else None
    if raw is None:
        return timeout
    try:
        return max(1, min(int(raw), _MAX_FETCH_TIMEOUT))
    except (TypeError, ValueError):
        return timeout


async def _web_fetch_impl(url: str) -> str:
    jina_client = JinaClient()
    timeout = _resolve_fetch_timeout()
    https_proxy = get_tool_https_proxy("web_fetch")
    html_content = await jina_client.crawl(url, return_format="html", timeout=timeout, https_proxy=https_proxy)

    jina_error: str | None = None
    if isinstance(html_content, str) and html_content.startswith("Error:"):
        jina_error = html_content
        fallback_content = await _fetch_direct(url, timeout=timeout, https_proxy=https_proxy)
        if isinstance(fallback_content, str) and fallback_content.startswith("Error:"):
            # Both providers failed — surface a structured JSON error so the
            # agent can reason about the failure mode instead of parsing prose.
            return json.dumps(
                {
                    "error": "web_fetch failed",
                    "url": url,
                    "jina_error": jina_error,
                    "fallback_error": fallback_content,
                },
                ensure_ascii=False,
            )
        html_content = fallback_content

    article = await asyncio.to_thread(readability_extractor.extract_article, html_content)
    markdown = article.to_markdown()
    if len(markdown) > _MAX_OUTPUT_CHARS:
        # Loud signal so operators notice when truncation routinely fires —
        # otherwise downstream agents see partial pages with no indication.
        logger.info(
            "web_fetch truncated output for %s: %d -> %d chars",
            url, len(markdown), _MAX_OUTPUT_CHARS,
        )
    return markdown[:_MAX_OUTPUT_CHARS]


def _create_web_fetch_tool() -> StructuredTool:
    """Create the web_fetch tool with both sync and async invocation support.

    The core implementation is async. A sync wrapper bridges via asyncio.run()
    for contexts where the graph executes synchronously (e.g., agent.stream()).
    This fixes: "StructuredTool does not support sync invocation" — the
    `@tool` decorator on an async-only function left `func=None`, causing
    LangGraph's ToolNode to fail when it calls `tool.invoke()`.
    """

    def _sync_web_fetch(url: str) -> str:
        """Sync wrapper that delegates to the async implementation."""
        coro = _web_fetch_impl(url)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # Already in an async context — schedule on a background thread
            # to avoid nested event-loop issues.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result()
        else:
            return asyncio.run(coro)

    return StructuredTool(
        name="web_fetch",
        description=_WEB_FETCH_DESCRIPTION,
        args_schema=_WebFetchInput,
        func=_sync_web_fetch,
        coroutine=_web_fetch_impl,
    )


# Export the web_fetch tool as a StructuredTool instance.
web_fetch_tool = _create_web_fetch_tool()
