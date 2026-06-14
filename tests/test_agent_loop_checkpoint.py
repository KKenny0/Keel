from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from keel_runtime import (
    AgentLoop,
    AgentLoopCheckpoint,
    AgentLoopCheckpointStatus,
    AgentLoopConfig,
    AgentSpec,
    JobManager,
    PrefixStableContext,
    ToolRegistry,
    tool,
)
from keel_runtime.events import utc_now


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


def build_checkpoint(job_id: str = "job-1") -> AgentLoopCheckpoint:
    now = utc_now()
    return AgentLoopCheckpoint(
        version=1,
        job_id=job_id,
        attempt_id="attempt-1",
        agent_name="research-agent",
        iteration=2,
        status=AgentLoopCheckpointStatus.AWAITING_TOOL,
        history_messages=[{"role": "user", "content": "task"}],
        active_messages=[{"role": "assistant", "content": "calling tool"}],
        pending_tool_calls=[{"name": "lookup", "arguments": {"query": "keel"}}],
        completed_tool_results=[{"name": "memory_record", "ok": True}],
        context_results=[{"tokens_used": 12, "trimmed_count": 0}],
        composed_prompts=[{"content": "system"}],
        gate_decisions=[{"request_id": "gate-1", "status": "approved"}],
        raw_output=None,
        parsed_output={"answer": "done"},
        parse_error=None,
        created_at=now,
        updated_at=now,
    )


def test_agent_loop_checkpoint_dict_round_trip() -> None:
    checkpoint = build_checkpoint()

    data = checkpoint.to_dict()
    restored = AgentLoopCheckpoint.from_dict(json.loads(json.dumps(data)))

    assert restored.to_dict() == data
    assert restored.status == AgentLoopCheckpointStatus.AWAITING_TOOL
    assert restored.pending_tool_calls[0]["name"] == "lookup"


def test_agent_loop_checkpoint_store_round_trip(tmp_path: Path) -> None:
    manager = JobManager(root=tmp_path)
    job_id = manager.create_job(AgentSpec(name="writer"), {"message": "checkpoint"})
    stores = manager.stores
    checkpoint = build_checkpoint(job_id)

    stores.checkpoints.save(checkpoint)

    assert stores.checkpoints.exists(job_id) is True
    assert stores.checkpoints.load(job_id).to_dict() == checkpoint.to_dict()
    assert stores.build_record(job_id)["checkpoints"]["agent_loop"] == checkpoint.to_dict()


def test_agent_loop_checkpoint_sink_records_tool_boundaries() -> None:
    @tool(name="lookup")
    def lookup(query: str) -> str:
        return f"found:{query}"

    checkpoints: list[AgentLoopCheckpoint] = []
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
        }
    )
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=1_000),
        ToolRegistry([lookup]),
        AgentLoopConfig(
            job_id="job-1",
            agent_name="research-agent",
            max_iterations=1,
        ),
    )

    result = run(
        loop.run(
            "question",
            attempt_id="attempt-1",
            checkpoint_sink=checkpoints.append,
        )
    )

    assert result.status == "max_iterations"
    awaiting = next(
        checkpoint
        for checkpoint in checkpoints
        if checkpoint.status == AgentLoopCheckpointStatus.AWAITING_TOOL
    )
    assert awaiting.pending_tool_calls[0]["name"] == "lookup"
    completed = next(
        checkpoint
        for checkpoint in checkpoints
        if checkpoint.status == AgentLoopCheckpointStatus.RUNNING
        and checkpoint.completed_tool_results
    )
    assert completed.completed_tool_results[0]["output"] == "found:keel"
    assert completed.pending_tool_calls == []


def test_agent_loop_resumes_after_completed_tool_without_rerunning_tool() -> None:
    calls = {"lookup": 0}

    @tool(name="lookup")
    def lookup(query: str) -> str:
        calls["lookup"] += 1
        return f"found:{query}"

    first_checkpoints: list[AgentLoopCheckpoint] = []
    first_client = FakeChatClient(
        {
            "content": "checking",
            "tool_calls": [
                {
                    "name": "lookup",
                    "arguments": {"query": "keel"},
                    "call_id": "call-1",
                }
            ],
        }
    )
    first_loop = AgentLoop(
        first_client,
        PrefixStableContext(max_tokens=1_000),
        ToolRegistry([lookup]),
        AgentLoopConfig(job_id="job-1", max_iterations=1),
    )

    first_result = run(
        first_loop.run(
            "question",
            attempt_id="attempt-1",
            checkpoint_sink=first_checkpoints.append,
        )
    )
    checkpoint = next(
        item
        for item in reversed(first_checkpoints)
        if item.status == AgentLoopCheckpointStatus.RUNNING
        and item.completed_tool_results
        and not item.pending_tool_calls
    )

    resumed_checkpoints: list[AgentLoopCheckpoint] = []
    resumed_client = FakeChatClient({"content": "final answer"})
    resumed_loop = AgentLoop(
        resumed_client,
        PrefixStableContext(max_tokens=1_000),
        ToolRegistry([lookup]),
        AgentLoopConfig(job_id="job-1"),
    )

    resumed_result = run(
        resumed_loop.run(
            "ignored input",
            attempt_id="attempt-1",
            checkpoint_source=checkpoint,
            checkpoint_sink=resumed_checkpoints.append,
        )
    )

    assert first_result.status == "max_iterations"
    assert resumed_result.status == "succeeded"
    assert resumed_result.output == "final answer"
    assert calls["lookup"] == 1
    assert len(resumed_client.calls) == 1
    sent_messages = resumed_client.calls[0]["messages"]
    assert any(message.role == "tool" and message.name == "lookup" for message in sent_messages)
    assert all(message.content != "ignored input" for message in sent_messages)
    assert resumed_checkpoints[-1].status == AgentLoopCheckpointStatus.COMPLETED


def test_agent_loop_checkpoint_rejects_invalid_identity() -> None:
    now = utc_now()

    try:
        AgentLoopCheckpoint(
            version=1,
            job_id="",
            attempt_id="attempt-1",
            agent_name="agent",
            iteration=0,
            status=AgentLoopCheckpointStatus.RUNNING,
            created_at=now,
            updated_at=now,
        )
    except ValueError as exc:
        assert "job_id" in str(exc)
    else:
        raise AssertionError("expected ValueError")
