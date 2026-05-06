"""Example 01 — Hello World."""

from __future__ import annotations

import asyncio
import os

from pydantic import BaseModel

from deerflow_sdk import (
    Harness,
    Hook,
    HookContext,
    RunComplete,
    TextDelta,
    ToolCall,
    ModelConfig,
    tool,
)


@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"Sunny in {city}, 22°C"


class LogToolCalls(Hook):
    async def pre_tool_use(self, ctx: HookContext, tool_name: str, tool_input: dict[str, object]) -> None:
        print(f"[hook] -> {tool_name}({tool_input})")


class WeatherReport(BaseModel):
    city: str
    summary: str
    temperature_c: float


async def main() -> None:
    api_key = os.getenv("DEERFLOW_EXAMPLE_API_KEY")
    model = (
        ModelConfig(
            name="qwen3.6-plus",
            provider="dashscope",
            extra={
                "api_key": api_key,
            },
        )
        if api_key
        else ModelConfig(name="fake", provider="fake")
    )

    harness = Harness(
        model=model,
        tools=[get_weather],
        hooks=[LogToolCalls()],
    )

    async with harness:
        text: str = await harness.run("What's the weather in Shanghai?")
        print("text:", text)

        report: WeatherReport = await harness.run(
            "Return JSON only for the weather in Beijing with keys city, summary, temperature_c.",
            output_type=WeatherReport,
        )
        print("report:", report)

        async for event in harness.stream("Weather in Tokyo?"):
            match event:
                case TextDelta(delta=d):
                    print(d, end="", flush=True)
                case ToolCall(tool_name=name, input=inp):
                    print(f"\n→ tool: {name}({inp})")
                case RunComplete(final_output=out):
                    print(f"\n=== done: {out}")


if __name__ == "__main__":
    asyncio.run(main())
