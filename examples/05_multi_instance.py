"""Example 05 — Multiple Harness instances in one process.

This is the *defining* property of a real harness framework: you can
construct two independent harnesses with different models, different
tools, different sandboxes, and run them concurrently without any
crosstalk. The legacy ``DeerFlowClient`` cannot do this because of
module-level globals (settings, _client_manager, _background_tasks,
ThreadPoolExecutors, the IPv4 monkey-patch).

If this example ever fails — if state from harness A leaks into harness B
— that is a release-blocking bug.
"""

from __future__ import annotations

import asyncio

from deerflow_sdk import Harness, tool


@tool
def echo_a(text: str) -> str:
    """Echo prefixed with A."""
    return f"A says: {text}"


@tool
def echo_b(text: str) -> str:
    """Echo prefixed with B."""
    return f"B says: {text}"


async def main() -> None:
    ha = Harness(model="qwen3.6-plus", tools=[echo_a])
    hb = Harness(model="claude-sonnet-4-7", tools=[echo_b])

    # Sanity: each harness only sees its own tools, in its own config.
    assert {t.name for t in ha.tools} == {"echo_a"}
    assert {t.name for t in hb.tools} == {"echo_b"}
    assert ha.config.model == "qwen3.6-plus"
    assert hb.config.model == "claude-sonnet-4-7"
    assert ha is not hb

    async with ha, hb:
        # Phase 2 will replace these prints with real concurrent runs.
        results = await asyncio.gather(
            _safe_run(ha, "hello from A"),
            _safe_run(hb, "hello from B"),
        )
        for label, result in zip(["A", "B"], results):
            print(f"{label} -> {result}")


async def _safe_run(h: Harness, prompt: str) -> str:
    try:
        return await h.run(prompt)
    except NotImplementedError as exc:
        return f"<contract stub: {exc}>"


if __name__ == "__main__":
    asyncio.run(main())
