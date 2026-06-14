from __future__ import annotations

import json
import zipfile
from pathlib import Path

from keel_runtime import (
    AgentLoopCheckpointStatus,
    AgentSpec,
    InMemoryObjectStorage,
    JobAttempt,
    JobAttemptKind,
    JobAttemptStatus,
    JobManager,
)
from keel_runtime.events import utc_now
from keel_runtime.jobs import AgentLoopCheckpoint


def build_attempt(job_id: str = "job-1") -> JobAttempt:
    return JobAttempt(
        id="attempt-1",
        job_id=job_id,
        number=1,
        kind=JobAttemptKind.INITIAL,
        status=JobAttemptStatus.RUNNING,
        started_at=utc_now(),
        idempotency_key="idem-1",
        retryable=None,
    )


def test_job_attempt_dict_round_trip() -> None:
    attempt = build_attempt()

    data = attempt.to_dict()
    restored = JobAttempt.from_dict(json.loads(json.dumps(data)))

    assert restored.to_dict() == data
    assert restored.kind == JobAttemptKind.INITIAL
    assert restored.status == JobAttemptStatus.RUNNING


def test_job_attempt_store_lists_attempts(tmp_path: Path) -> None:
    manager = JobManager(root=tmp_path)
    job_id = manager.create_job(AgentSpec(name="writer"), {"message": "attempt"})
    stores = manager.stores
    attempt = build_attempt(job_id)

    stores.attempts.save(attempt)

    assert stores.attempts.load(job_id, "attempt-1").to_dict() == attempt.to_dict()
    assert [item.id for item in stores.attempts.list(job_id)] == ["attempt-1"]
    assert stores.build_record(job_id)["attempts"] == [attempt.to_dict()]


def test_job_record_defaults_to_initial_attempt_and_no_checkpoints(tmp_path: Path) -> None:
    manager = JobManager(root=tmp_path)
    job_id = manager.create_job(AgentSpec(name="writer"), {"message": "legacy"})

    record = manager.stores.build_record(job_id)

    assert record["attempts"][0]["id"] == "attempt-1"
    assert record["attempts"][0]["kind"] == JobAttemptKind.INITIAL.value
    assert record["attempts"][0]["status"] == JobAttemptStatus.CREATED.value
    assert record["checkpoints"] == {}


def test_job_attempt_rejects_invalid_number() -> None:
    try:
        JobAttempt(
            id="attempt-1",
            job_id="job-1",
            number=0,
            kind=JobAttemptKind.INITIAL,
            status=JobAttemptStatus.CREATED,
            started_at=utc_now(),
        )
    except ValueError as exc:
        assert "number" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_attempts_and_checkpoints_are_exported_and_restored(tmp_path: Path) -> None:
    storage = InMemoryObjectStorage()
    first = JobManager(root=tmp_path / "first", object_storage=storage)
    job_id = first.create_job(AgentSpec(name="writer"), {"message": "persist"})
    attempt = build_attempt(job_id)
    checkpoint = AgentLoopCheckpoint(
        version=1,
        job_id=job_id,
        attempt_id=attempt.id,
        agent_name="writer",
        iteration=1,
        status=AgentLoopCheckpointStatus.RUNNING,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    first.stores.attempts.save(attempt)
    first.stores.checkpoints.save(checkpoint)

    export_path = first.export_job(job_id)
    uploaded = set(first.sync_job(job_id))

    with zipfile.ZipFile(export_path) as archive:
        names = set(archive.namelist())

    assert "attempts/attempt-1.json" in names
    assert "checkpoints/agent-loop.json" in names
    assert f"jobs/{job_id}/attempts/attempt-1.json" in uploaded
    assert f"jobs/{job_id}/checkpoints/agent-loop.json" in uploaded

    second = JobManager(root=tmp_path / "second", object_storage=storage)
    second.restore_job(job_id)

    assert second.stores.attempts.load(job_id, attempt.id).to_dict() == attempt.to_dict()
    assert second.stores.checkpoints.load(job_id).to_dict() == checkpoint.to_dict()
