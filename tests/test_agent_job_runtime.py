from __future__ import annotations

import asyncio
import json
from typing import Any

from keel_runtime import (
    AgentLoopCheckpoint,
    AgentLoopCheckpointStatus,
    EventType,
    JobManager,
    JobStatus,
    Message,
    ToolResult,
    agent,
    tool,
)
from keel_runtime.events import utc_now


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


def test_resume_restorable_continues_from_checkpoint_without_rerunning_tool(
    tmp_path,
) -> None:
    tool_calls = {"lookup": 0}

    @tool(name="lookup")
    def lookup(query: str) -> str:
        tool_calls["lookup"] += 1
        return f"found:{query}"

    client = FakeChatClient({"content": "final answer"})

    @agent(client=client, tools=[lookup], max_tokens=1_000)
    def writer(topic: str) -> str:
        return topic

    manager = JobManager(root=tmp_path)
    job_id = manager.create_agent_job(writer, "topic")
    now = utc_now()
    tool_result = ToolResult.success("lookup", "found:topic", call_id="call-1")
    checkpoint = AgentLoopCheckpoint(
        version=1,
        job_id=job_id,
        attempt_id="attempt-1",
        agent_name="writer",
        iteration=1,
        status=AgentLoopCheckpointStatus.RUNNING,
        history_messages=[],
        active_messages=[
            Message(role="user", content="topic").to_dict(),
            Message(
                role="assistant",
                content="checking",
                metadata={
                    "tool_calls": [
                        {
                            "name": "lookup",
                            "arguments": {"query": "topic"},
                            "call_id": "call-1",
                        }
                    ]
                },
            ).to_dict(),
            Message(
                role="tool",
                content=tool_result.to_dict(),
                name="lookup",
                metadata={"tool_result": True, "tool_call_id": "call-1"},
            ).to_dict(),
        ],
        completed_tool_results=[tool_result.to_dict()],
        created_at=now,
        updated_at=now,
    )
    manager.stores.checkpoints.save(checkpoint)
    job = manager.get_job(job_id)
    job.with_status(JobStatus.RESTORABLE)
    manager.stores.jobs.save(job)

    events = collect(manager.resume_restorable(job_id))

    assert manager.get_status(job_id) == JobStatus.SUCCEEDED
    assert tool_calls["lookup"] == 0
    assert any(
        event.type == EventType.OUTPUT and event.message == "final answer"
        for event in events
    )
    assert manager.download_artifact(job_id, "result.txt") == b"final answer"


def test_retry_failed_agent_job_reuses_registered_agent(tmp_path) -> None:
    @tool(name="lookup")
    def lookup(query: str) -> str:
        return f"found:{query}"

    client = FakeChatClient(
        {
            "content": "checking",
            "tool_calls": [
                {
                    "name": "lookup",
                    "arguments": {"query": "topic"},
                    "call_id": "call-1",
                }
            ],
        },
        {"content": "retry answer"},
    )

    @agent(client=client, tools=[lookup], max_tokens=1_000, max_iterations=1)
    def writer(topic: str) -> str:
        return topic

    manager = JobManager(root=tmp_path)
    failed_id = manager.create_agent_job(writer, "topic")
    collect(manager.stream(failed_id))
    assert manager.get_status(failed_id) == JobStatus.FAILED

    retry_id = manager.retry_failed(failed_id)
    events = collect(manager.stream(retry_id))

    assert manager.get_status(retry_id) == JobStatus.SUCCEEDED
    assert any(
        event.type == EventType.OUTPUT and event.message == "retry answer"
        for event in events
    )
