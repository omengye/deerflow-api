import asyncio
import concurrent.futures
import json
import logging

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

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


_DEFAULT_FETCH_TIMEOUT = 10
_MAX_FETCH_TIMEOUT = 120
_DEFAULT_MAX_OUTPUT_CHARS = 4096
_MAX_MAX_OUTPUT_CHARS = 50000


def _resolve_fetch_timeout() -> int:
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


def _resolve_max_output_chars() -> int:
    try:
        config = get_app_config().get_tool_config("web_fetch")
    except Exception:
        return _DEFAULT_MAX_OUTPUT_CHARS
    if config is None:
        return _DEFAULT_MAX_OUTPUT_CHARS
    raw = config.model_extra.get("max_output_chars") if config.model_extra else None
    if raw is None:
        return _DEFAULT_MAX_OUTPUT_CHARS
    try:
        return max(256, min(int(raw), _MAX_MAX_OUTPUT_CHARS))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_OUTPUT_CHARS


def _scrapling_fetch(url: str, timeout: int, https_proxy: str | None) -> str:
    """Sync Scrapling fetch using curl_cffi-based Fetcher with browser impersonation."""
    from scrapling.fetchers import Fetcher

    try:
        kwargs: dict = {"timeout": timeout}
        if https_proxy:
            kwargs["proxy"] = https_proxy
        response = Fetcher.get(url, **kwargs)
        html = response.html_content
        if not html or not html.strip():
            logger.error("Scrapling returned empty response for %s", url)
            return "Error: Scrapling returned empty response"
        return html
    except Exception as e:
        logger.warning("Scrapling fetch failed for %s: %s", url, type(e).__name__)
        return f"Error: Scrapling fetch failed: {type(e).__name__}"


async def _fetch_direct(url: str, timeout: int, https_proxy: str | None = None) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
    }
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout, headers=headers,
            proxy=https_proxy, trust_env=False,
        ) as client:
            response = await client.get(url)
        if response.status_code >= 400:
            preview = (response.text or "")[:500]
            return f"Error: Direct fetch returned status {response.status_code}: {preview}"
        return response.text
    except Exception as e:
        return f"Error: Direct fetch failed: {type(e).__name__}"


async def _web_fetch_impl(url: str) -> str:
    timeout = _resolve_fetch_timeout()
    https_proxy = get_tool_https_proxy("web_fetch")

    html_content = await asyncio.to_thread(_scrapling_fetch, url, timeout, https_proxy)

    if isinstance(html_content, str) and html_content.startswith("Error:"):
        scrapling_error = html_content
        fallback_content = await _fetch_direct(url, timeout=timeout, https_proxy=https_proxy)
        if isinstance(fallback_content, str) and fallback_content.startswith("Error:"):
            return json.dumps(
                {
                    "error": "web_fetch failed",
                    "url": url,
                    "scrapling_error": scrapling_error,
                    "fallback_error": fallback_content,
                },
                ensure_ascii=False,
            )
        html_content = fallback_content

    article = await asyncio.to_thread(readability_extractor.extract_article, html_content)
    markdown = article.to_markdown()
    max_output_chars = _resolve_max_output_chars()
    if len(markdown) > max_output_chars:
        logger.info(
            "web_fetch truncated output for %s: %d -> %d chars",
            url, len(markdown), max_output_chars,
        )
    return markdown[:max_output_chars]


def _create_web_fetch_tool() -> StructuredTool:
    def _sync_web_fetch(url: str) -> str:
        coro = _web_fetch_impl(url)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
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


web_fetch_tool = _create_web_fetch_tool()
