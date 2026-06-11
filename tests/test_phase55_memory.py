from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from keel_runtime import (
    AgentLoop,
    AgentLoopConfig,
    Decision,
    LocalMemoryProvider,
    PrefixStableContext,
    ToolRegistry,
    memory_tools,
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


def test_decision_round_trips_and_local_provider_persists_jsonl(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memory.jsonl"
    provider = LocalMemoryProvider(path)
    decision = Decision.create(
        "Choose local memory",
        "Use JSONL for the default provider",
        scope="keel",
        rationale="keeps core dependencies at zero",
        tags=["runtime", "memory"],
        metadata={"phase": "5.5.7"},
    )

    recorded = run(provider.record_decision(decision))
    reloaded = LocalMemoryProvider(path)

    assert recorded.to_dict()["title"] == "Choose local memory"
    assert reloaded.list_decisions(scope="keel")[0].to_dict() == decision.to_dict()


def test_local_memory_provider_recalls_by_keyword_and_scope() -> None:
    provider = LocalMemoryProvider()
    run(
        provider.record_decision(
            Decision.create(
                "Agent loop memory",
                "Register memory tools automatically",
                scope="agent",
                tags=["loop"],
            )
        )
    )
    run(
        provider.record_decision(
            Decision.create(
                "Docs memory",
                "Keep research notes separate",
                scope="docs",
                tags=["notes"],
            )
        )
    )

    agent_hits = run(provider.recall("automatically tools", scope="agent"))
    docs_hits = run(provider.recall("automatically tools", scope="docs"))

    assert [decision.scope for decision in agent_hits] == ["agent"]
    assert agent_hits[0].title == "Agent loop memory"
    assert docs_hits == []


def test_local_memory_provider_returns_recent_scope_records_for_empty_query() -> None:
    provider = LocalMemoryProvider()
    first = run(provider.record_decision(Decision.create("First", "one", scope="keel")))
    second = run(provider.record_decision(Decision.create("Second", "two", scope="keel")))

    results = run(provider.recall("", scope="keel", limit=2))

    assert [decision.id for decision in results] == [second.id, first.id]


def test_memory_tools_record_and_recall_decisions() -> None:
    provider = LocalMemoryProvider()
    registry = ToolRegistry(memory_tools(provider, default_scope="keel"))

    record_result = run(
        registry.execute(
            "memory_record",
            {
                "title": "Keep scope local",
                "outcome": "Scope separates project memories",
                "rationale": "prevents unrelated recall",
                "tags": ["scope"],
            },
        )
    )
    recall_result = run(registry.execute("memory_recall", {"query": "scope local"}))

    assert record_result.ok is True
    assert record_result.output["scope"] == "keel"
    assert recall_result.ok is True
    assert recall_result.output[0]["title"] == "Keep scope local"
    assert recall_result.output[0]["scope"] == "keel"


def test_agent_loop_without_memory_provider_does_not_register_memory_tools() -> None:
    client = FakeChatClient({"content": "ok"})
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=1_000),
        config=AgentLoopConfig(system_prompt="system"),
    )

    result = run(loop.run("question"))

    assert result.status == "succeeded"
    assert all(tool["name"] != "memory_recall" for tool in client.calls[0]["tools"])
    assert all(tool["name"] != "memory_record" for tool in client.calls[0]["tools"])


def test_agent_loop_auto_registers_memory_tools_and_agent_can_read_write() -> None:
    provider = LocalMemoryProvider()
    client = FakeChatClient(
        {
            "tool_calls": [
                {
                    "name": "memory_record",
                    "arguments": {
                        "title": "Use tools",
                        "outcome": "AgentLoop registers memory tools",
                        "tags": ["agent-loop"],
                    },
                }
            ],
        },
        {
            "tool_calls": [
                {
                    "name": "memory_recall",
                    "arguments": {"query": "registers memory"},
                }
            ],
        },
        {"content": "memory complete"},
    )
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=1_000),
        config=AgentLoopConfig(memory_provider=provider, memory_scope="keel"),
    )

    result = run(loop.run("remember the tool decision"))

    assert result.status == "succeeded"
    assert result.output == "memory complete"
    assert provider.list_decisions(scope="keel")[0].title == "Use tools"
    assert result.tool_results[0].name == "memory_record"
    assert result.tool_results[1].name == "memory_recall"
    assert result.tool_results[1].output[0]["title"] == "Use tools"
    assert any(tool["name"] == "memory_recall" for tool in client.calls[0]["tools"])
    assert any(tool["name"] == "memory_record" for tool in client.calls[0]["tools"])


def test_local_memory_provider_documents_keyword_only_behavior() -> None:
    assert "keyword" in (LocalMemoryProvider.__doc__ or "").lower()
    assert "not semantic search" in (LocalMemoryProvider.__doc__ or "").lower()
