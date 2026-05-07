"""Patched ChatOpenAI for DashScope that captures reasoning_content from streaming responses.

DashScope's OpenAI-compatible API returns thinking/reasoning content in the
``reasoning_content`` field of stream delta objects (e.g. for qwen3-thinking or
deepseek-v3 with enable_thinking=true).  Standard LangChain ``ChatOpenAI`` only
processes the ``content`` field and silently drops ``reasoning_content``.

This module provides ``PatchedDashScopeChatOpenAI`` which overrides
``_convert_chunk_to_generation_chunk`` to capture ``reasoning_content`` from
each delta and store it in ``AIMessageChunk.additional_kwargs["reasoning_content"]``.
That makes it available downstream for AG-UI thinking events.

Usage in ``config.yaml``::

    - name: qwen3.6-plus
      display_name: Qwen 3.6 Plus
      use: deerflow.models.patched_dashscope:PatchedDashScopeChatOpenAI
      model: deepseek-v4-pro
      api_key: $DASHSCOPE_API_KEY
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      supports_thinking: true
      when_thinking_enabled:
        extra_body:
          enable_thinking: true
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI


class PatchedDashScopeChatOpenAI(ChatOpenAI):
    """ChatOpenAI with ``reasoning_content`` capture for DashScope thinking models.

    When ``enable_thinking: true`` is passed via ``extra_body``, DashScope includes
    a ``reasoning_content`` field alongside ``content`` in each stream delta.
    This class captures that field and stores it in
    ``AIMessageChunk.additional_kwargs["reasoning_content"]`` so that downstream
    AG-UI serialisers can emit ``REASONING_MESSAGE_*`` events.
    """

    @classmethod
    def is_lc_serializable(cls) -> bool:
        return True

    @property
    def lc_secrets(self) -> dict[str, str]:
        return {"api_key": "OPENAI_API_KEY"}

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict[str, Any],
        default_chunk_class: type,
        base_generation_info: dict[str, Any] | None,
    ) -> ChatGenerationChunk | None:
        """Convert a raw stream chunk, also capturing ``reasoning_content``.

        Delegates to the parent implementation then enriches the resulting
        ``AIMessageChunk`` with any ``reasoning_content`` found in the delta.
        """
        gen_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if gen_chunk is None:
            return None

        choices = (
            chunk.get("choices", [])
            or chunk.get("chunk", {}).get("choices", [])
        )
        if not choices:
            return gen_chunk

        delta: dict[str, Any] = choices[0].get("delta") or {}
        reasoning_content: str | None = delta.get("reasoning_content")
        if reasoning_content and isinstance(gen_chunk.message, AIMessageChunk):
            existing = gen_chunk.message.additional_kwargs.get("reasoning_content", "")
            gen_chunk.message.additional_kwargs["reasoning_content"] = existing + reasoning_content

        return gen_chunk
