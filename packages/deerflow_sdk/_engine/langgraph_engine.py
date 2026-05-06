from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from deerflow_sdk._config import HarnessConfig, ModelConfig
from deerflow_sdk._errors import ModelError, PermissionDenied
from deerflow_sdk._events import RunComplete, StreamEvent, SubagentEnd, SubagentStart, ToolResult
from deerflow_sdk._hooks import Hook, HookContext
from deerflow_sdk._permissions import AskUserFn, Permission, PermissionContext, PermissionDecision
from deerflow_sdk._sandbox.base import Sandbox
from deerflow_sdk._subagents import SubagentSpec
from deerflow_sdk._tools import Tool, ToolContext

from deerflow_sdk._engine.event_adapter import StreamState, complete_event, events_from_stream_item


class LangGraphEngine:
    def __init__(
        self,
        *,
        config: HarnessConfig,
        tools: tuple[Tool, ...],
        subagents: tuple[SubagentSpec, ...],
        hooks: tuple[Hook, ...],
        permissions: tuple[Permission, ...],
        ask_user: AskUserFn | None = None,
        sandbox: Sandbox | None,
    ) -> None:
        self._config = config
        self._tools = tools
        self._subagents = subagents
        self._hooks = hooks
        self._permissions = permissions
        self._ask_user = ask_user
        self._sandbox = sandbox

    async def run(self, prompt: str, *, thread_id: str | None, output_type: type[BaseModel] | None) -> BaseModel | str:
        final: RunComplete | None = None
        async for event in self.stream(prompt, thread_id=thread_id, output_type=output_type):
            if isinstance(event, RunComplete):
                final = event
        if final is None:
            raise ModelError("agent run finished without a completion event")
        return cast(BaseModel | str, final.final_output)

    async def stream(
        self,
        prompt: str,
        *,
        thread_id: str | None,
        output_type: type[BaseModel] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        from langchain_core.messages import HumanMessage
        from langchain_core.runnables import RunnableConfig

        actual_thread_id = thread_id or str(uuid4())
        run_id = str(uuid4())
        hook_ctx = HookContext(run_id=run_id, thread_id=actual_thread_id)

        try:
            for hook in self._hooks:
                await hook.on_run_start(hook_ctx, prompt)
        except Exception as exc:
            yield ToolResult(tool_name="hook", tool_call_id="run_start", error=f"hook.on_run_start failed: {exc}")
            if hook_ctx.abort:
                return

        actual_prompt = prompt
        try:
            for hook in self._hooks:
                replacement = await hook.on_user_prompt(hook_ctx, actual_prompt)
                if replacement is not None:
                    actual_prompt = replacement
        except Exception as exc:
            yield ToolResult(tool_name="hook", tool_call_id="user_prompt", error=f"hook.on_user_prompt failed: {exc}")
            if hook_ctx.abort:
                return

        stream_state = StreamState()
        state: dict[str, Any] = {"messages": [HumanMessage(content=actual_prompt)]}
        runnable_config = RunnableConfig(
            configurable={"thread_id": actual_thread_id, "model_name": self._model_name(), "thinking_enabled": False},
            recursion_limit=self._config.max_iterations,
        )

        event_queue: asyncio.Queue[StreamEvent | BaseException | None] = asyncio.Queue()
        graph = self._build_graph(
            run_id=run_id,
            thread_id=actual_thread_id,
            hook_ctx=hook_ctx,
            event_queue=event_queue,
        )

        async def _produce() -> None:
            try:
                async for item in graph.astream(
                    state,
                    config=runnable_config,
                    context={"thread_id": actual_thread_id},
                    stream_mode=["values", "messages"],
                ):
                    for event in events_from_stream_item(item, stream_state):
                        await event_queue.put(event)
                    if hook_ctx.abort:
                        break

                final_output: BaseModel | str = stream_state.final_output
                if output_type is not None:
                    try:
                        final_output = _parse_structured_output(output_type, stream_state.final_output)
                    except Exception as exc:
                        await event_queue.put(ToolResult(tool_name="engine", tool_call_id="parse", error=str(exc)))

                try:
                    for hook in self._hooks:
                        await hook.on_run_end(hook_ctx, final_output)
                except Exception as exc:
                    await event_queue.put(ToolResult(tool_name="hook", tool_call_id="run_end", error=f"hook.on_run_end failed: {exc}"))

                await event_queue.put(complete_event(stream_state, final_output))
            except BaseException as exc:
                await event_queue.put(exc)
            finally:
                await event_queue.put(None)

        producer = asyncio.create_task(_produce())
        try:
            while True:
                event = await event_queue.get()
                if event is None:
                    break
                if isinstance(event, BaseException):
                    raise event
                yield event
        finally:
            await producer

    def _build_graph(
        self,
        *,
        run_id: str,
        thread_id: str,
        hook_ctx: HookContext,
        event_queue: asyncio.Queue[StreamEvent | BaseException | None],
    ) -> Any:
        from typing import NotRequired, Required

        import langchain.agents as agents_module
        import langchain.agents.middleware.types as middleware_types
        from langchain.agents import AgentState, create_agent

        for module in (agents_module, middleware_types):
            if not hasattr(module, "Required"):
                setattr(module, "Required", Required)
            if not hasattr(module, "NotRequired"):
                setattr(module, "NotRequired", NotRequired)

        model = self._create_model()
        sdk_tools: list[Tool] = [*self._tools, *_subagent_dispatch_tools(self, run_id=run_id, thread_id=thread_id, event_queue=event_queue)]
        lc_tools = [
            _to_langchain_tool(
                t,
                run_id=run_id,
                thread_id=thread_id,
                sandbox=self._sandbox,
                hook_ctx=hook_ctx,
                hooks=self._hooks,
                permissions=self._permissions,
                ask_user=self._ask_user,
            )
            for t in sdk_tools
        ]
        return create_agent(
            model=model,
            tools=lc_tools,
            system_prompt=self._config.system_prompt,
            state_schema=AgentState,
            checkpointer=None,
            name="deerflow_sdk",
        )

    def _create_model(self) -> Any:
        model = self._config.model
        if isinstance(model, str):
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(model=model)
        if model.provider == "fake":
            return _FakeChatModel()
        if model.provider in (None, "openai", "dashscope"):
            from langchain_openai import ChatOpenAI

            kwargs = _trusted_provider_kwargs(model)
            if model.provider == "dashscope":
                kwargs["base_url"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            if model.temperature is not None:
                kwargs["temperature"] = model.temperature
            if model.max_tokens is not None:
                kwargs["max_tokens"] = model.max_tokens
            return ChatOpenAI(model=model.name, **kwargs)
        if model.provider == "anthropic":
            from langchain_anthropic import ChatAnthropic

            kwargs = _trusted_provider_kwargs(model)
            if model.temperature is not None:
                kwargs["temperature"] = model.temperature
            if model.max_tokens is not None:
                kwargs["max_tokens"] = model.max_tokens
            return ChatAnthropic(model_name=model.name, **kwargs)
        raise ModelError(f"unsupported model provider {model.provider!r}")

    def _model_name(self) -> str:
        model = self._config.model
        return model if isinstance(model, str) else model.name


def _to_langchain_tool(
    tool: Tool,
    *,
    run_id: str,
    thread_id: str,
    sandbox: Sandbox | None,
    hook_ctx: HookContext,
    hooks: tuple[Hook, ...],
    permissions: tuple[Permission, ...],
    ask_user: AskUserFn | None,
) -> Any:
    from langchain_core.tools import StructuredTool

    async def _call(**kwargs: Any) -> Any:
        permission_ctx = PermissionContext(run_id=run_id, thread_id=thread_id, ask_user=ask_user)
        for permission in permissions:
            decision = await permission.check(permission_ctx, tool.name, kwargs)
            if decision is PermissionDecision.DENY:
                raise PermissionDenied(tool.name, "permission returned deny")
            if decision is PermissionDecision.ASK_USER:
                allowed = (
                    await permission_ctx.ask_user(tool.name, "Approve tool call?", kwargs)
                    if permission_ctx.ask_user
                    else False
                )
                if not allowed:
                    raise PermissionDenied(tool.name, "user did not approve tool call")

        error: Exception | None = None
        output: Any = None
        try:
            for hook in hooks:
                await hook.pre_tool_use(hook_ctx, tool.name, kwargs)
        except Exception:
            pass

        try:
            output = await tool(ToolContext(run_id=run_id, thread_id=thread_id, sandbox=sandbox), **kwargs)
            return output
        except Exception as exc:
            error = exc
            raise
        finally:
            try:
                for hook in hooks:
                    await hook.post_tool_use(hook_ctx, tool.name, kwargs, output, error)
            except Exception:
                pass

    return StructuredTool.from_function(
        coroutine=_call,
        name=tool.name,
        description=tool.description,
        args_schema=tool.input_schema,
    )


def _parse_structured_output(output_type: type[BaseModel], text: str) -> BaseModel:
    try:
        return output_type.model_validate_json(text)
    except Exception:
        try:
            return output_type.model_validate({"text": text})
        except Exception as exc:
            raise ModelError(f"model output could not be parsed as {output_type.__name__}: {exc}") from exc


def _trusted_provider_kwargs(model: ModelConfig) -> dict[str, Any]:
    blocked = {
        "api_base",
        "base_url",
        "default_headers",
        "http_async_client",
        "http_client",
        "openai_api_base",
        "proxies",
        "proxy",
        "transport",
    }
    unsafe = blocked.intersection(model.extra)
    if unsafe:
        raise ModelError(f"unsafe provider kwargs are not accepted in ModelConfig.extra: {', '.join(sorted(unsafe))}")
    return dict(model.extra)


class _FakeChatModel:  # pragma: no cover - exercised through example smoke tests
    def __new__(cls) -> Any:
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, ToolMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        class FakeChatModel(BaseChatModel):
            bound_tools: Any = None

            @property
            def _llm_type(self) -> str:
                return "deerflow-sdk-fake"

            def _generate(self, messages: list[Any], stop: list[str] | None = None, run_manager: Any | None = None, **kwargs: Any) -> ChatResult:
                for message in reversed(messages):
                    if isinstance(message, ToolMessage):
                        content = f"Parent received: {message.content}"
                        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])
                prompt = str(getattr(messages[-1], "content", "")) if messages else ""
                dispatch_tool = next(
                    (tool for tool in (self.bound_tools or []) if str(getattr(tool, "name", "")).startswith("dispatch_")),
                    None,
                )
                if dispatch_tool is not None and "delegate" in prompt.lower():
                    tool_name = getattr(dispatch_tool, "name", "")
                    return ChatResult(
                        generations=[
                            ChatGeneration(
                                message=AIMessage(
                                    content="",
                                    tool_calls=[{"name": tool_name, "args": {"prompt": prompt}, "id": "call_fake_subagent"}],
                                )
                            )
                        ]
                    )
                if "JSON" in prompt or "keys city" in prompt:
                    content = '{"city":"Beijing","summary":"Sunny in Beijing, 22°C","temperature_c":22.0}'
                elif "Tokyo" in prompt:
                    content = "Sunny in Tokyo, 22°C"
                else:
                    content = "Sunny in Shanghai, 22°C"
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

            def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
                return self.model_copy(update={"bound_tools": list(tools or [])})

        return FakeChatModel()


class _SubagentDispatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str


class _SubagentDispatchTool:
    name: str
    description: str
    input_schema: type[BaseModel]

    def __init__(
        self,
        *,
        parent: LangGraphEngine,
        spec: SubagentSpec,
        run_id: str,
        thread_id: str,
        event_queue: asyncio.Queue[StreamEvent | BaseException | None],
    ) -> None:
        self._parent = parent
        self._spec = spec
        self._run_id = run_id
        self._thread_id = thread_id
        self._event_queue = event_queue
        self.name = f"dispatch_{spec.name}"
        self.description = spec.description
        self.input_schema = _SubagentDispatchInput

    async def __call__(self, ctx: ToolContext, /, **kwargs: Any) -> str:
        prompt = str(kwargs["prompt"])
        child_run_id = str(uuid4())
        child_thread_id = f"{self._thread_id}:subagent:{self._spec.name}:{child_run_id}"
        await self._event_queue.put(SubagentStart(subagent_name=self._spec.name, run_id=child_run_id, prompt=prompt))
        child_config = HarnessConfig(
            model=self._spec.model or self._parent._config.model,
            system_prompt=self._spec.system_prompt,
            max_iterations=self._spec.max_iterations,
        )
        child = LangGraphEngine(
            config=child_config,
            tools=self._spec.tools,
            subagents=(),
            hooks=self._parent._hooks,
            permissions=self._parent._permissions,
            ask_user=self._parent._ask_user,
            sandbox=self._parent._sandbox,
        )
        try:
            output = await child.run(prompt, thread_id=child_thread_id, output_type=None)
        except Exception as exc:
            await self._event_queue.put(SubagentEnd(subagent_name=self._spec.name, run_id=child_run_id, error=str(exc)))
            raise
        await self._event_queue.put(SubagentEnd(subagent_name=self._spec.name, run_id=child_run_id, output=output))
        return str(output)


def _subagent_dispatch_tools(
    parent: LangGraphEngine,
    *,
    run_id: str,
    thread_id: str,
    event_queue: asyncio.Queue[StreamEvent | BaseException | None],
) -> list[_SubagentDispatchTool]:
    _validate_subagents(parent._tools, parent._subagents)
    return [
        _SubagentDispatchTool(parent=parent, spec=spec, run_id=run_id, thread_id=thread_id, event_queue=event_queue)
        for spec in parent._subagents
    ]


def _validate_subagents(parent_tools: tuple[Tool, ...], subagents: tuple[SubagentSpec, ...]) -> None:
    seen: set[str] = set()
    parent_tool_names = {tool.name for tool in parent_tools}
    for spec in subagents:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", spec.name):
            raise ValueError(f"invalid subagent name {spec.name!r}")
        dispatch_name = f"dispatch_{spec.name}"
        if dispatch_name in seen:
            raise ValueError(f"duplicate subagent dispatch tool {dispatch_name!r}")
        if dispatch_name in parent_tool_names:
            raise ValueError(f"subagent dispatch tool {dispatch_name!r} collides with a parent tool")
        seen.add(dispatch_name)
