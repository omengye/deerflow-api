"""Provider-neutral extraction of model-visible response text."""

from __future__ import annotations


def extract_response_text(content: object) -> str:
    """Extract only text-bearing blocks from common LLM response shapes.

    Responses API messages may contain reasoning and tool blocks alongside
    ``text`` or ``output_text`` blocks.  Structured parsers must not treat the
    auxiliary blocks as the model's final answer, especially at a security
    decision boundary.
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") not in {"text", "output_text"}:
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)
