"""Phase 1 contract tests for the public deerflow_sdk API.

Goal: lock the v0.1 public surface so we cannot accidentally break it
while filling in the engine. These tests intentionally do NOT exercise
the agent loop; they assert SHAPE only.

If any of these tests fail, the public API contract has changed —
update CHANGELOG.md and bump the version per SemVer.
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest


# ---------------------------------------------------------------------
# Public surface: every symbol in __all__ must be importable and stable.
# ---------------------------------------------------------------------

EXPECTED_PUBLIC = {
    "Harness",
    "HarnessConfig",
    "ModelConfig",
    "tool",
    "Tool",
    "ToolContext",
    "subagent",
    "SubagentSpec",
    "Hook",
    "HookContext",
    "Permission",
    "PermissionDecision",
    "PermissionContext",
    "Sandbox",
    "LocalSandbox",
    "StreamEvent",
    "TextDelta",
    "ToolCall",
    "ToolResult",
    "SubagentStart",
    "SubagentEnd",
    "RunComplete",
    "DeerFlowError",
    "ToolError",
    "PermissionDenied",
    "SandboxError",
    "ModelError",
}


def test_public_api_complete() -> None:
    import deerflow_sdk

    assert set(deerflow_sdk.__all__) == EXPECTED_PUBLIC
    for name in EXPECTED_PUBLIC:
        assert hasattr(deerflow_sdk, name), f"missing public symbol: {name}"


def test_version_set() -> None:
    import deerflow_sdk

    assert isinstance(deerflow_sdk.__version__, str)
    assert deerflow_sdk.__version__.startswith("0.1.")


# ---------------------------------------------------------------------
# Zero-leakage: the user-facing module must NOT import langgraph/langchain.
# ---------------------------------------------------------------------

def test_no_langgraph_in_public_module() -> None:
    """The public facade must not transitively import langgraph or langchain.

    Engine adapters live in deerflow_sdk._engine and are loaded lazily,
    only when Harness.run/stream is actually invoked.
    """
    import sys

    # Drop any cached modules first so the import is observable.
    for mod in list(sys.modules):
        if mod.startswith(("deerflow_sdk", "langgraph", "langchain")):
            del sys.modules[mod]

    import deerflow_sdk  # noqa: F401

    leaked = [m for m in sys.modules if m.startswith(("langgraph", "langchain"))]
    assert leaked == [], f"public import leaked engine modules: {leaked}"


# ---------------------------------------------------------------------
# @tool decorator: signature + schema inference.
# ---------------------------------------------------------------------

def test_tool_decorator_infers_schema_from_annotations() -> None:
    from deerflow_sdk import Tool, tool

    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    assert isinstance(add, Tool)
    assert add.name == "add"
    assert "Add two numbers" in add.description
    schema = add.input_schema
    fields = schema.model_fields
    assert set(fields) == {"a", "b"}


def test_tool_requires_description() -> None:
    from deerflow_sdk import tool

    with pytest.raises(ValueError, match="description required"):

        @tool
        def no_doc(x: int) -> int:  # noqa: D401, D103
            return x


def test_tool_requires_type_annotations() -> None:
    from deerflow_sdk import tool

    with pytest.raises(TypeError, match="no type annotation"):

        @tool
        def untyped(x):  # type: ignore[no-untyped-def]
            """Untyped."""
            return x


# ---------------------------------------------------------------------
# Harness: shape only (no engine yet).
# ---------------------------------------------------------------------

def test_harness_requires_model_or_config() -> None:
    from deerflow_sdk import Harness

    with pytest.raises(TypeError, match="model.*required"):
        Harness()


def test_harness_rejects_both_model_and_config() -> None:
    from deerflow_sdk import Harness, HarnessConfig

    with pytest.raises(TypeError, match="not both"):
        Harness(model="qwen3.6-plus", config=HarnessConfig(model="x"))


def test_harness_state_is_per_instance() -> None:
    """Two harnesses in the same process must be fully independent."""
    from deerflow_sdk import Harness, tool

    @tool
    def t1(x: int) -> int:
        """one."""
        return x

    @tool
    def t2(x: int) -> int:
        """two."""
        return x

    a = Harness(model="m1", tools=[t1])
    b = Harness(model="m2", tools=[t2])

    assert a is not b
    assert a.config.model == "m1"
    assert b.config.model == "m2"
    assert {t.name for t in a.tools} == {"t1"}
    assert {t.name for t in b.tools} == {"t2"}


def test_harness_run_signature_stable() -> None:
    """run() / stream() signatures are part of the contract."""
    from deerflow_sdk import Harness

    run_sig = inspect.signature(Harness.run)
    assert list(run_sig.parameters) == ["self", "prompt", "thread_id", "output_type"]
    assert run_sig.parameters["thread_id"].kind == inspect.Parameter.KEYWORD_ONLY
    assert run_sig.parameters["output_type"].kind == inspect.Parameter.KEYWORD_ONLY

    stream_sig = inspect.signature(Harness.stream)
    assert list(stream_sig.parameters) == ["self", "prompt", "thread_id"]


@pytest.mark.asyncio
async def test_harness_run_works_with_fake_model() -> None:
    from deerflow_sdk import Harness, ModelConfig

    h = Harness(model=ModelConfig(name="fake", provider="fake"))
    assert await h.run("hi") == "Sunny in Shanghai, 22°C"


def test_stream_adapter_accumulates_final_output_without_duplicate_text() -> None:
    from langchain_core.messages import AIMessage, AIMessageChunk

    from deerflow_sdk import RunComplete, TextDelta
    from deerflow_sdk._engine.event_adapter import StreamState, complete_event, events_from_stream_item

    state = StreamState()
    events = [
        *events_from_stream_item(("messages", (AIMessageChunk(content="hel", id="m1"), {})), state),
        *events_from_stream_item(("messages", (AIMessageChunk(content="lo", id="m1"), {})), state),
        *events_from_stream_item(("values", {"messages": [AIMessage(content="hello", id="m1")]}), state),
        complete_event(state),
    ]

    assert [event.delta for event in events if isinstance(event, TextDelta)] == ["hel", "lo"]
    complete = events[-1]
    assert isinstance(complete, RunComplete)
    assert complete.final_output == "hello"


def test_stream_adapter_deduplicates_tool_results() -> None:
    from langchain_core.messages import ToolMessage

    from deerflow_sdk import ToolResult
    from deerflow_sdk._engine.event_adapter import StreamState, events_from_stream_item

    state = StreamState()
    tool_msg = ToolMessage(content="ok", name="lookup", tool_call_id="tc1", id="tm1")
    events = [
        *events_from_stream_item(("messages", (tool_msg, {})), state),
        *events_from_stream_item(("values", {"messages": [tool_msg]}), state),
    ]

    assert [event for event in events if isinstance(event, ToolResult)] == [ToolResult(tool_call_id="tc1", tool_name="lookup", output="ok")]


@pytest.mark.asyncio
async def test_tool_decorator_passes_ctx_when_declared() -> None:
    from deerflow_sdk import ToolContext, tool

    @tool
    def needs_ctx(ctx: ToolContext, value: str) -> str:
        """Read thread id from context."""
        return f"{ctx.thread_id}:{value}"

    assert await needs_ctx(ToolContext(run_id="r", thread_id="t"), value="x") == "t:x"


@pytest.mark.asyncio
async def test_harness_stream_emits_subagent_events() -> None:
    from deerflow_sdk import Harness, ModelConfig, RunComplete, SubagentEnd, SubagentSpec, SubagentStart

    h = Harness(
        model=ModelConfig(name="fake", provider="fake"),
        subagents=[SubagentSpec(name="researcher", description="Research things.", model=ModelConfig(name="fake", provider="fake"))],
    )

    events = [event async for event in h.stream("delegate this to a researcher")]
    assert any(isinstance(event, SubagentStart) and event.subagent_name == "researcher" for event in events)
    assert any(isinstance(event, SubagentEnd) and event.subagent_name == "researcher" and event.output for event in events)
    assert isinstance(events[-1], RunComplete)
    assert events[-1].final_output.startswith("Parent received:")


@pytest.mark.asyncio
async def test_harness_run_returns_parent_output_with_subagent() -> None:
    from deerflow_sdk import Harness, ModelConfig, SubagentSpec

    h = Harness(
        model=ModelConfig(name="fake", provider="fake"),
        subagents=[SubagentSpec(name="researcher", description="Research things.", model=ModelConfig(name="fake", provider="fake"))],
    )

    assert await h.run("delegate this") == "Parent received: Sunny in Shanghai, 22°C"


@pytest.mark.asyncio
async def test_subagent_dispatch_is_wrapped_by_permissions_and_hooks() -> None:
    from deerflow_sdk import Harness, Hook, HookContext, ModelConfig, Permission, PermissionContext, PermissionDecision, SubagentSpec

    seen: list[str] = []

    class RecordPermission(Permission):
        async def check(self, ctx: PermissionContext, tool_name: str, tool_input: dict[str, object]) -> PermissionDecision:
            seen.append(f"permission:{tool_name}")
            return PermissionDecision.ALLOW

    class RecordHook(Hook):
        async def pre_tool_use(self, ctx: HookContext, tool_name: str, tool_input: dict[str, object]) -> None:
            seen.append(f"pre:{tool_name}")

        async def post_tool_use(self, ctx: HookContext, tool_name: str, tool_input: dict[str, object], output: object, error: Exception | None) -> None:
            seen.append(f"post:{tool_name}")

    h = Harness(
        model=ModelConfig(name="fake", provider="fake"),
        subagents=[SubagentSpec(name="researcher", description="Research things.", model=ModelConfig(name="fake", provider="fake"))],
        permissions=[RecordPermission()],
        hooks=[RecordHook()],
    )

    await h.run("delegate this")
    assert "permission:dispatch_researcher" in seen
    assert "pre:dispatch_researcher" in seen
    assert "post:dispatch_researcher" in seen


def test_subagent_names_are_validated() -> None:
    import asyncio

    from deerflow_sdk import Harness, HookContext, ModelConfig, SubagentSpec, tool

    with pytest.raises(ValueError, match="invalid subagent name"):
        h = Harness(
            model=ModelConfig(name="fake", provider="fake"),
            subagents=[SubagentSpec(name="bad-name", description="bad")],
        )
        h._ensure_engine()._build_graph(run_id="r", thread_id="t", hook_ctx=HookContext(run_id="r", thread_id="t"), event_queue=asyncio.Queue())

    @tool
    def dispatch_researcher(prompt: str) -> str:
        """Colliding parent dispatch tool."""
        return prompt

    with pytest.raises(ValueError, match="collides"):
        h = Harness(
            model=ModelConfig(name="fake", provider="fake"),
            tools=[dispatch_researcher],
            subagents=[SubagentSpec(name="researcher", description="Research things.")],
        )
        h._ensure_engine()._build_graph(run_id="r", thread_id="t", hook_ctx=HookContext(run_id="r", thread_id="t"), event_queue=asyncio.Queue())


@pytest.mark.asyncio
async def test_model_config_rejects_network_sensitive_extra() -> None:
    from deerflow_sdk import Harness, ModelConfig, ModelError

    h = Harness(model=ModelConfig(name="m", provider="openai", extra={"base_url": "http://127.0.0.1"}))
    with pytest.raises(ModelError, match="unsafe provider kwargs"):
        await h.run("hi")


@pytest.mark.asyncio
async def test_harness_aclose_is_idempotent() -> None:
    from deerflow_sdk import Harness

    h = Harness(model="any")
    await h.aclose()
    await h.aclose()  # second call must not raise


@pytest.mark.asyncio
async def test_harness_async_context_manager() -> None:
    from deerflow_sdk import Harness

    async with Harness(model="any") as h:
        assert h.config.model == "any"


# ---------------------------------------------------------------------
# Sandbox: secure-by-default contract.
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_local_sandbox_blocks_bash_by_default() -> None:
    """The defining security property: LocalSandbox.execute() refuses
    shell execution unless allow_host_bash=True is explicitly opted in."""
    from deerflow_sdk import LocalSandbox, SandboxError

    sb = LocalSandbox()
    with pytest.raises(SandboxError, match="host bash execution is disabled"):
        await sb.execute("echo hello")


# ---------------------------------------------------------------------
# Events: discriminator field is stable.
# ---------------------------------------------------------------------

def test_event_discriminators() -> None:
    from deerflow_sdk import (
        RunComplete,
        SubagentEnd,
        SubagentStart,
        TextDelta,
        ToolCall,
        ToolResult,
    )

    expected = {
        TextDelta: "text_delta",
        ToolCall: "tool_call",
        ToolResult: "tool_result",
        SubagentStart: "subagent_start",
        SubagentEnd: "subagent_end",
        RunComplete: "run_complete",
    }
    for cls, discriminator in expected.items():
        instance_type = get_type_hints(cls)["type"]
        # Literal["..."] -> __args__ == ("...",)
        assert instance_type.__args__ == (discriminator,)


# ---------------------------------------------------------------------
# Hook / Permission: subclassable with default no-op behavior.
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hook_default_methods_are_noop() -> None:
    from deerflow_sdk import Hook, HookContext

    h = Hook()
    ctx = HookContext(run_id="r", thread_id="t")
    await h.on_run_start(ctx, "p")
    assert await h.on_user_prompt(ctx, "p") is None
    await h.pre_tool_use(ctx, "x", {})
    await h.post_tool_use(ctx, "x", {}, None, None)
    await h.on_run_end(ctx, None)


@pytest.mark.asyncio
async def test_permission_default_allows() -> None:
    from deerflow_sdk import Permission, PermissionContext, PermissionDecision

    p = Permission()
    ctx = PermissionContext(run_id="r", thread_id="t")
    assert await p.check(ctx, "any_tool", {}) == PermissionDecision.ALLOW


# ---------------------------------------------------------------------
# Subagent decorator.
# ---------------------------------------------------------------------

def test_subagent_decorator_produces_spec() -> None:
    from deerflow_sdk import SubagentSpec, subagent, tool

    @tool
    def search(q: str) -> str:
        """Search."""
        return q

    @subagent(name="researcher", description="A researcher.", tools=[search])
    class _Researcher:
        system_prompt = "You research things."

    assert isinstance(_Researcher, SubagentSpec)
    assert _Researcher.name == "researcher"
    assert _Researcher.system_prompt == "You research things."
    assert _Researcher.tools[0].name == "search"
