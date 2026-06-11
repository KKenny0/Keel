from __future__ import annotations

import asyncio
from typing import Any

from keel_runtime import (
    AgentLoop,
    AgentLoopConfig,
    EventType,
    Message,
    PrefixStableContext,
    ToolRegistry,
    tool,
)


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


def test_agent_loop_completes_direct_answer_and_parses_output() -> None:
    client = FakeChatClient(
        {"content": '{"answer": "done"}', "usage": {"total_tokens": 12}}
    )
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=1_000),
        ToolRegistry(),
        AgentLoopConfig(system_prompt="system"),
    )

    result = run(loop.run("question"))

    assert result.status == "succeeded"
    assert result.output == {"answer": "done"}
    assert result.raw_output == '{"answer": "done"}'
    assert result.iterations == 1
    assert client.calls[0]["messages"][0].content == "system"
    assert client.calls[0]["tools"] == []
    assert any(event.message == "agent loop usage recorded" for event in result.events)
    assert any(event.type == EventType.OUTPUT for event in result.events)


def test_agent_loop_executes_tool_call_and_sends_result_to_next_round() -> None:
    @tool(name="lookup")
    def lookup(query: str) -> str:
        return f"found:{query}"

    client = FakeChatClient(
        {
            "content": "checking",
            "tool_calls": [
                {
                    "name": "lookup",
                    "arguments": {"query": "keel"},
                    "call_id": "call-1",
                }
            ],
        },
        {"content": "final answer"},
    )
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=1_000),
        ToolRegistry([lookup]),
        AgentLoopConfig(system_prompt="system"),
    )

    result = run(loop.run("question"))

    assert result.status == "succeeded"
    assert result.output == "final answer"
    assert result.iterations == 2
    assert result.tool_results[0].ok is True
    assert result.tool_results[0].output == "found:keel"
    second_messages = client.calls[1]["messages"]
    tool_message = next(message for message in second_messages if message.role == "tool")
    assert tool_message.name == "lookup"
    assert tool_message.content["output"] == "found:keel"
    assert any(event.message == "tool call completed" for event in result.events)


def test_agent_loop_can_feed_tool_error_back_to_model() -> None:
    @tool(name="explode")
    def explode() -> str:
        raise RuntimeError("boom")

    client = FakeChatClient(
        {"tool_calls": [{"name": "explode", "arguments": {}}]},
        {"content": "handled error"},
    )
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=1_000),
        ToolRegistry([explode]),
    )

    result = run(loop.run("question"))

    assert result.status == "succeeded"
    assert result.output == "handled error"
    assert result.tool_results[0].ok is False
    assert result.tool_results[0].error == "boom"
    assert any(event.type == EventType.ERROR for event in result.events)


def test_agent_loop_can_fail_fast_on_tool_error() -> None:
    @tool(name="explode")
    def explode() -> str:
        raise RuntimeError("boom")

    client = FakeChatClient({"tool_calls": [{"name": "explode", "arguments": {}}]})
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=1_000),
        ToolRegistry([explode]),
        AgentLoopConfig(fail_on_tool_error=True),
    )

    result = run(loop.run("question"))

    assert result.status == "failed"
    assert result.error == "boom"
    assert result.iterations == 1
    assert len(client.calls) == 1


def test_agent_loop_reports_max_iterations_when_tool_calls_do_not_finish() -> None:
    @tool(name="lookup")
    def lookup() -> str:
        return "still working"

    client = FakeChatClient({"tool_calls": [{"name": "lookup", "arguments": {}}]})
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=1_000),
        ToolRegistry([lookup]),
        AgentLoopConfig(max_iterations=1),
    )

    result = run(loop.run("question"))

    assert result.status == "max_iterations"
    assert result.error == "Maximum iterations reached: 1"
    assert result.iterations == 1
    assert result.tool_results[0].output == "still working"
    assert any(event.message == "Maximum iterations reached: 1" for event in result.events)


def test_agent_loop_uses_context_provider_before_each_model_call() -> None:
    client = FakeChatClient({"content": "ok"})
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=24, keep_recent_turns=0),
        ToolRegistry(),
        AgentLoopConfig(system_prompt="system"),
    )
    history = [
        Message(role="user", content="task"),
        Message(role="assistant", content="first"),
        Message(role="user", content="old " + ("x" * 120)),
    ]

    result = run(loop.run("latest", history=history))

    assert result.status == "succeeded"
    assert result.context_results[0].trimmed_count == 1
    sent_contents = [str(message.content) for message in client.calls[0]["messages"]]
    assert sent_contents[:3] == ["system", "task", "first"]
    assert "old " + ("x" * 120) not in sent_contents
    assert "latest" in sent_contents
