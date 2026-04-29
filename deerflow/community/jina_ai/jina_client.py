import logging
import os
import threading

import httpx

logger = logging.getLogger(__name__)

# Module-global flag guarded by a lock so concurrent first-time callers do not
# race past the warning check together.
_api_key_warned = False
_api_key_warn_lock = threading.Lock()

_MAX_TIMEOUT = 120  # hard cap so caller mistakes cannot hang event loops indefinitely
_ERROR_BODY_PREVIEW = 500  # avoid spilling huge upstream payloads into logs/responses


def _warn_missing_api_key_once() -> None:
    global _api_key_warned
    with _api_key_warn_lock:
        if _api_key_warned:
            return
        _api_key_warned = True
    logger.warning(
        "Jina API key is not set. Provide your own key to access a higher rate "
        "limit. See https://jina.ai/reader for more information."
    )


class JinaClient:
    async def crawl(
        self,
        url: str,
        return_format: str = "html",
        timeout: int = 10,
        https_proxy: str | None = None,
    ) -> str:
        # Clamp timeout to a sane range — non-positive values would otherwise
        # raise inside httpx and abort the request immediately.
        try:
            timeout_int = max(1, min(int(timeout), _MAX_TIMEOUT))
        except (TypeError, ValueError):
            timeout_int = 10

        headers = {
            "Content-Type": "application/json",
            "X-Return-Format": return_format,
            "X-Timeout": str(timeout_int),
        }
        api_key = os.getenv("JINA_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            _warn_missing_api_key_once()

        data = {"url": url}
        try:
            async with httpx.AsyncClient(proxy=https_proxy, trust_env=False) as client:
                response = await client.post(
                    "https://r.jina.ai/", headers=headers, json=data, timeout=timeout_int
                )

            if response.status_code != 200:
                preview = (response.text or "")[:_ERROR_BODY_PREVIEW]
                # Log full status; only echo a truncated, generic message back to
                # the caller so we do not leak upstream server-side detail.
                logger.error(
                    "Jina API returned status %s for %s (body preview=%r)",
                    response.status_code, url, preview,
                )
                return f"Error: Jina API returned status {response.status_code}"

            if not response.text or not response.text.strip():
                logger.error("Jina API returned empty response for %s", url)
                return "Error: Jina API returned empty response"

            return response.text
        except Exception as e:
            logger.warning(
                "Request to Jina API failed for %s: %s", url, type(e).__name__
            )
            return f"Error: Request to Jina API failed: {type(e).__name__}"
