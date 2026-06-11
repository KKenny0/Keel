from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from keel_runtime import Agent, agent


def load_quickstart_example() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "examples" / "quickstart_agent.py"
    spec = importlib.util.spec_from_file_location("quickstart_agent_example", path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load quickstart example")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(coro):
    return asyncio.run(coro)


class FakeChatClient:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools):
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        if not self.responses:
            raise AssertionError("unexpected chat call")
        return self.responses.pop(0)


def test_agent_decorator_turns_function_output_into_loop_input() -> None:
    client = FakeChatClient({"content": "accepted"})

    @agent(
        client=client,
        system_prompt="system",
        max_tokens=1_000,
    )
    def reviewer(question: str) -> dict[str, str]:
        return {"question": question}

    result = run(reviewer("check this"))

    assert isinstance(reviewer, Agent)
    assert reviewer.__name__ == "reviewer"
    assert result.status == "succeeded"
    assert result.output == "accepted"
    assert client.calls[0]["messages"][0].content == "system"
    assert client.calls[0]["messages"][1].content == {"question": "check this"}


def test_quickstart_example_runs_agent_tool_and_memory_path() -> None:
    quickstart = load_quickstart_example()
    result, decisions = run(quickstart.run_quickstart())

    assert result.status == "succeeded"
    assert result.output == "Weather report: Tokyo: 22C, sunny"
    assert [tool_result.name for tool_result in result.tool_results] == [
        "memory_record",
        "get_weather",
    ]
    assert decisions[0].title == "Weather quickstart"
    assert decisions[0].scope == "quickstart"
