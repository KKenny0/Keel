"""Runnable 5-minute quickstart for Keel's @agent API."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import keel_runtime as keel  # noqa: E402


@keel.tool(name="get_weather", description="Get current weather for a city")
def get_weather(city: str) -> str:
    return f"{city}: 22C, sunny"


class QuickstartClient:
    """Mock chat client so the example runs without a real model provider."""

    async def chat(self, messages, tools) -> dict[str, Any] | str:
        tool_names = {message.name for message in messages if message.role == "tool"}
        if "memory_record" not in tool_names:
            return {
                "tool_calls": [
                    {
                        "name": "memory_record",
                        "arguments": {
                            "title": "Weather quickstart",
                            "outcome": "Use get_weather for forecast questions",
                            "tags": ["quickstart"],
                        },
                    }
                ]
            }
        if "get_weather" not in tool_names:
            return {
                "tool_calls": [
                    {
                        "name": "get_weather",
                        "arguments": {"city": "Tokyo"},
                    }
                ]
            }

        weather = next(
            message.content["output"]
            for message in messages
            if message.role == "tool" and message.name == "get_weather"
        )
        return f"Weather report: {weather}"


def build_weather_agent() -> tuple[keel.Agent, keel.LocalMemoryProvider]:
    memory = keel.LocalMemoryProvider()

    @keel.agent(
        client=QuickstartClient(),
        tools=[get_weather],
        memory=memory,
        memory_scope="quickstart",
        system_prompt="You are a concise weather assistant.",
        max_iterations=5,
        max_tokens=4_000,
    )
    async def weather_agent(question: str) -> str:
        return question

    return weather_agent, memory


async def run_quickstart() -> tuple[keel.AgentLoopResult, list[keel.Decision]]:
    weather_agent, memory = build_weather_agent()
    result = await weather_agent("What is the weather in Tokyo?")
    return result, memory.list_decisions(scope="quickstart")


async def main() -> None:
    result, decisions = await run_quickstart()
    print(result.status)
    print(result.output)
    print(decisions[0].title)


if __name__ == "__main__":
    asyncio.run(main())
