import inspect
import logging
from typing import Any

from .factory import create_chat_model

logger = logging.getLogger(__name__)

__all__ = ["create_chat_model", "aclose_chat_model"]


async def aclose_chat_model(model: Any) -> None:
    """Drain a chat model's underlying async HTTP clients.

    Call this in ``finally`` blocks around model usage whose surrounding
    event loop is about to be closed (worker-thread ``asyncio.run`` paths
    in particular). Without it, openai/httpx connection pools retain SSL
    transports bound to the dying loop; later GC or pool reuse then
    surfaces as ``RuntimeError: Event loop is closed`` inside an unrelated
    coroutine.

    Safe to call on any model — unknown shapes are silently ignored.
    """
    if model is None:
        return

    seen: set[int] = set()
    # langchain_openai.ChatOpenAI: ``root_async_client`` is the
    # ``openai.AsyncOpenAI`` instance and ``http_async_client`` is a
    # user-supplied ``httpx.AsyncClient`` (when one was injected).
    for attr in ("root_async_client", "http_async_client", "_async_client"):
        client = getattr(model, attr, None)
        if client is None or id(client) in seen:
            continue
        seen.add(id(client))

        closer = getattr(client, "close", None) or getattr(client, "aclose", None)
        if closer is None:
            continue
        try:
            result = closer()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug(
                "Failed to close chat model client attribute %r",
                attr,
                exc_info=True,
            )
