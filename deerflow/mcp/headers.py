"""Shared validation for MCP HTTP header values.

MCP transports pass ``dict[str, str]`` values through httpx and h11.  Values
outside their common boundary must be rejected before either library builds an
exception that can flow through tool error handling into model-visible state.
"""

from __future__ import annotations

import re


_FORBIDDEN_HEADER_VALUE_CHARS = re.compile(r"[\x00\n\x0b\x0c\r]")


def illegal_header_value_reason(value: str) -> str | None:
    """Return a non-sensitive reason when *value* cannot be transported."""
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return "contains characters outside ASCII"

    if _FORBIDDEN_HEADER_VALUE_CHARS.search(value):
        return "contains a line break or another forbidden control character"
    if value != value.strip(" \t"):
        return "has leading or trailing whitespace"
    return None
