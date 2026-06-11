from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from keel_runtime import (
    AgentLoop,
    AgentLoopConfig,
    AgentSpec,
    CollaborationStatus,
    CollaborationStepStatus,
    EventType,
    GateDecision,
    GateDecisionStatus,
    GateRequest,
    HumanGate,
    JobEvent,
    JobManager,
    JobStatus,
    PrefixStableContext,
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


def test_gate_request_and_decision_round_trip() -> None:
    request = GateRequest.create(
        "deploy change",
        reason="needs approval",
        payload={"branch": "main"},
        timeout_seconds=5,
    )
    decision = GateDecision.approve(
        request.id,
        feedback="ship it",
        payload={"reviewer": "human"},
    )

    restored_request = GateRequest.from_dict(request.to_dict())
    restored_decision = GateDecision.from_dict(decision.to_dict())

    assert restored_request.action == "deploy change"
    assert restored_request.payload == {"branch": "main"}
    assert restored_decision.approved is True
    assert restored_decision.status == GateDecisionStatus.APPROVED
    assert restored_decision.payload == {"reviewer": "human"}


def test_human_gate_records_request_decision_and_events() -> None:
    gate = HumanGate([GateDecision.reject("queued", feedback="not yet")])
    request = GateRequest.create("write file", reason="mutates workspace")

    decision = run(gate.request(request, job_id="job-1"))
    events = gate.drain_events()

    assert decision.rejected is True
    assert decision.request_id == request.id
    assert decision.feedback == "not yet"
    assert [event.message for event in events] == ["gate requested", "gate rejected"]
    assert events[0].data["request"]["action"] == "write file"


def test_human_gate_can_be_approved_while_request_is_pending() -> None:
    async def scenario() -> GateDecision:
        gate = HumanGate(default_timeout_seconds=1)
        task = asyncio.create_task(
            gate.request(GateRequest.create("continue"), job_id="job-1")
        )
        while not gate.requests:
            await asyncio.sleep(0)
        gate.approve(gate.requests[0].id, feedback="approved live")
        return await task

    decision = run(scenario())

    assert decision.approved is True
    assert decision.feedback == "approved live"


def test_agent_loop_exposes_human_gate_tool_and_continues_after_approval() -> None:
    gate = HumanGate([True])
    client = FakeChatClient(
        {
            "tool_calls": [
                {
                    "name": "human_gate",
                    "arguments": {
                        "action": "apply patch",
                        "reason": "before modifying files",
                    },
                    "call_id": "gate-1",
                }
            ],
        },
        {"content": "approved path complete"},
    )
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=1_000),
        config=AgentLoopConfig(human_gate=gate),
    )

    result = run(loop.run("make a guarded change"))

    assert result.status == "succeeded"
    assert result.output == "approved path complete"
    assert result.gate_decisions[0].approved is True
    assert any(tool["name"] == "human_gate" for tool in client.calls[0]["tools"])
    assert "gate requested" in [event.message for event in result.events]
    assert "gate approved" in [event.message for event in result.events]


def test_agent_loop_feeds_rejection_feedback_to_next_model_round_by_default() -> None:
    gate = HumanGate([GateDecision.reject("queued", feedback="revise first")])
    client = FakeChatClient(
        {
            "tool_calls": [
                {
                    "name": "human_gate",
                    "arguments": {"action": "publish answer"},
                }
            ],
        },
        {"content": "revised after rejection"},
    )
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=1_000),
        config=AgentLoopConfig(human_gate=gate),
    )

    result = run(loop.run("publish"))

    assert result.status == "succeeded"
    assert result.gate_decisions[0].rejected is True
    second_round_tool_message = next(
        message for message in client.calls[1]["messages"] if message.role == "tool"
    )
    assert second_round_tool_message.content["output"]["status"] == "rejected"
    assert second_round_tool_message.content["output"]["feedback"] == "revise first"


def test_agent_loop_can_fail_fast_on_gate_rejection() -> None:
    gate = HumanGate([GateDecision.reject("queued", feedback="stop")])
    client = FakeChatClient(
        {"tool_calls": [{"name": "human_gate", "arguments": {"action": "publish"}}]}
    )
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=1_000),
        config=AgentLoopConfig(
            human_gate=gate,
            fail_on_gate_rejection=True,
        ),
    )

    result = run(loop.run("publish"))

    assert result.status == "gate_rejected"
    assert result.error == "stop"
    assert result.gate_decisions[0].rejected is True
    assert any(event.type == EventType.ERROR for event in result.events)
    assert len(client.calls) == 1


def test_agent_loop_reports_gate_timeout_as_controlled_stop() -> None:
    gate = HumanGate(default_timeout_seconds=0)
    client = FakeChatClient(
        {"tool_calls": [{"name": "human_gate", "arguments": {"action": "publish"}}]}
    )
    loop = AgentLoop(
        client,
        PrefixStableContext(max_tokens=1_000),
        config=AgentLoopConfig(human_gate=gate),
    )

    result = run(loop.run("publish"))

    assert result.status == "gate_timeout"
    assert result.error == "human gate timed out"
    assert result.gate_decisions[0].timed_out is True
    assert "gate timeout" in [event.message for event in result.events]


class NoopRuntime:
    async def run(self, job, spec) -> AsyncIterator[JobEvent]:
        yield JobEvent.output(job.id, "ok")

    async def stop(self, job_id: str) -> None:
        return None


def test_collaboration_confirmation_behavior_is_unchanged(tmp_path) -> None:
    manager = JobManager(root=tmp_path, runtime=NoopRuntime())
    collaboration_id = manager.create_collaboration("confirm first")
    step_id = manager.add_collaboration_step(
        collaboration_id,
        AgentSpec(name="reviewed-agent"),
        {"mode": "guarded"},
        requires_confirmation=True,
    )

    step = manager.get_collaboration_step(collaboration_id, step_id)
    assert step.status == CollaborationStepStatus.WAITING_FOR_CONFIRMATION
    assert step.job_id is None
    assert manager.get_collaboration(collaboration_id).status == (
        CollaborationStatus.WAITING_FOR_CONFIRMATION
    )

    job_id = manager.confirm_collaboration_step(collaboration_id, step_id, note="approved")

    confirmed_step = manager.get_collaboration_step(collaboration_id, step_id)
    assert job_id is not None
    assert confirmed_step.job_id == job_id
    assert confirmed_step.confirmation_note == "approved"
    assert manager.get_job(job_id).status == JobStatus.CREATED
