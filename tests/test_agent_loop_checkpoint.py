from __future__ import annotations

import json
from pathlib import Path

from keel_runtime import AgentLoopCheckpoint, AgentLoopCheckpointStatus, AgentSpec, JobManager
from keel_runtime.events import utc_now


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
