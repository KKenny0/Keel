from __future__ import annotations

import asyncio
import json
from typing import Any

from keel_runtime import (
    AgentLoopCheckpointStatus,
    EventType,
    JobManager,
    JobStatus,
    agent,
    tool,
)


def collect(async_iter):
    async def _collect():
        return [event async for event in async_iter]

    return asyncio.run(_collect())


class FakeChatClient:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools):
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        if not self.responses:
            raise AssertionError("unexpected chat call")
        return self.responses.pop(0)


def test_decorated_agent_runs_as_persisted_job_with_checkpoint(tmp_path) -> None:
    tool_calls = {"lookup": 0}

    @tool(name="lookup")
    def lookup(query: str) -> str:
        tool_calls["lookup"] += 1
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
        {"content": '{"answer": "done"}'},
    )

    @agent(
        client=client,
        tools=[lookup],
        system_prompt="system",
        max_tokens=1_000,
    )
    def research(topic: str) -> str:
        return topic

    manager = JobManager(root=tmp_path)
    job_id = manager.create_agent_job(research, "keel")

    events = collect(manager.stream(job_id))

    assert manager.get_status(job_id) == JobStatus.SUCCEEDED
    assert tool_calls["lookup"] == 1
    assert any(event.message == "tool call completed" for event in events)
    assert any(event.type == EventType.OUTPUT for event in events)
    assert manager.download_artifact(job_id, "result.txt") == b'{"answer": "done"}'
    assert json.loads(manager.download_artifact(job_id, "output.json")) == {
        "answer": "done"
    }
    checkpoint = manager.stores.checkpoints.load(job_id)
    assert checkpoint.status == AgentLoopCheckpointStatus.COMPLETED
    assert checkpoint.raw_output == '{"answer": "done"}'
    assert checkpoint.parsed_output == {"answer": "done"}
    assert checkpoint.job_id == job_id


def test_agent_job_result_artifact_uses_final_output_only(tmp_path) -> None:
    client = FakeChatClient({"content": "final answer"})

    @agent(client=client, max_tokens=1_000)
    def writer(topic: str) -> str:
        return topic

    manager = JobManager(root=tmp_path)
    job_id = manager.create_agent_job(writer, "topic")

    collect(manager.stream(job_id))

    assert manager.download_artifact(job_id, "result.txt") == b"final answer"


def test_restarted_manager_marks_running_agent_job_restorable(tmp_path) -> None:
    client = FakeChatClient({"content": "final answer"})

    @agent(client=client, max_tokens=1_000)
    def writer(topic: str) -> str:
        return topic

    first = JobManager(root=tmp_path)
    job_id = first.create_agent_job(writer, "topic")
    job = first.get_job(job_id)
    job.with_status(JobStatus.RUNNING)
    first.stores.jobs.save(job)

    second = JobManager(root=tmp_path)

    assert second.get_status(job_id) == JobStatus.RESTORABLE
