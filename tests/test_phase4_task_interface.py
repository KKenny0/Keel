from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from keel_runtime import AgentSpec, ArtifactInput, EventType, JobEvent, JobManager, JobStatus
from keel_runtime.errors import JobNotFoundError


def collect(async_iter):
    async def _collect():
        return [event async for event in async_iter]

    return asyncio.run(_collect())


class WorkflowRuntime:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, job, spec) -> AsyncIterator[JobEvent]:
        self.calls.append(job.input["mode"])
        mode = job.input["mode"]
        if mode == "produce":
            yield JobEvent.output(job.id, job.input["payload"])
            return
        if mode == "consume":
            source = Path(job.workspace_path) / job.input["path"]
            yield JobEvent.output(job.id, f"consumed:{source.read_text(encoding='utf-8')}")
            return
        if mode == "optional":
            yield JobEvent.output(job.id, "optional-ok")
            return
        if mode == "fail":
            raise RuntimeError("producer failed")
        raise AssertionError(f"unknown mode: {mode}")

    async def stop(self, job_id: str) -> None:
        return None


def test_task_can_depend_on_previous_artifact(tmp_path: Path) -> None:
    manager = JobManager(root=tmp_path, runtime=WorkflowRuntime())
    producer_id = manager.create_task(
        AgentSpec(name="producer"),
        {"mode": "produce", "payload": "alpha"},
    )
    collect(manager.stream(producer_id))

    consumer_id = manager.create_task(
        AgentSpec(name="consumer"),
        {"mode": "consume", "path": "inputs/source.txt"},
        dependencies=[producer_id],
        artifact_inputs=[
            ArtifactInput(
                source_job_id=producer_id,
                source_path="result.txt",
                target_path="inputs/source.txt",
            )
        ],
    )

    events = collect(manager.stream(consumer_id))

    assert manager.get_status(consumer_id) == JobStatus.SUCCEEDED
    assert manager.download_artifact(consumer_id, "result.txt") == b"consumed:alpha"
    assert (Path(manager.get_job(consumer_id).workspace_path) / "inputs/source.txt").read_text(
        encoding="utf-8"
    ) == "alpha"
    assert any(event.message == "artifact input copied" for event in events)
    summary = manager.describe_job(consumer_id)
    assert summary["dependencies"] == [{"id": producer_id, "status": "succeeded", "error": None}]
    assert summary["artifacts"] == ["result.txt"]
    assert any(event["type"] == "output" for event in summary["logs"])


def test_failed_dependency_prevents_downstream_runtime_from_running(tmp_path: Path) -> None:
    runtime = WorkflowRuntime()
    manager = JobManager(root=tmp_path, runtime=runtime)
    producer_id = manager.create_task(AgentSpec(name="producer"), {"mode": "fail"})
    collect(manager.stream(producer_id))
    assert manager.get_status(producer_id) == JobStatus.FAILED

    consumer_id = manager.create_task(
        AgentSpec(name="consumer"),
        {"mode": "consume", "path": "inputs/source.txt"},
        dependencies=[producer_id],
    )

    events = collect(manager.stream(consumer_id))

    job = manager.get_job(consumer_id)
    assert job.status == JobStatus.FAILED
    assert f"dependency {producer_id} is failed" in (job.error or "")
    assert runtime.calls == ["fail"]
    assert not any(event.message == "job running" for event in manager.read_session(consumer_id))
    assert any(event.type == EventType.ERROR for event in events)


def test_dependency_relationship_survives_manager_restart(tmp_path: Path) -> None:
    runtime = WorkflowRuntime()
    first = JobManager(root=tmp_path, runtime=runtime)
    producer_id = first.create_task(
        AgentSpec(name="producer"),
        {"mode": "produce", "payload": "persisted"},
    )
    collect(first.stream(producer_id))
    consumer_id = first.create_task(
        AgentSpec(name="consumer"),
        {"mode": "consume", "path": "copied.txt"},
        artifact_inputs=[
            {
                "source_job_id": producer_id,
                "source_path": "result.txt",
                "target_path": "copied.txt",
            }
        ],
    )

    second = JobManager(root=tmp_path, runtime=runtime)
    collect(second.stream(consumer_id))

    assert second.get_status(consumer_id) == JobStatus.SUCCEEDED
    assert second.download_artifact(consumer_id, "result.txt") == b"consumed:persisted"
    assert second.get_job(consumer_id).dependencies == [producer_id]


def test_optional_missing_artifact_input_is_skipped(tmp_path: Path) -> None:
    manager = JobManager(root=tmp_path, runtime=WorkflowRuntime())
    producer_id = manager.create_task(
        AgentSpec(name="producer"),
        {"mode": "produce", "payload": "unused"},
    )
    collect(manager.stream(producer_id))
    consumer_id = manager.create_task(
        AgentSpec(name="consumer"),
        {"mode": "optional"},
        artifact_inputs=[
            ArtifactInput(
                source_job_id=producer_id,
                source_path="missing.txt",
                target_path="missing.txt",
                optional=True,
            )
        ],
    )

    events = collect(manager.stream(consumer_id))

    assert manager.get_status(consumer_id) == JobStatus.SUCCEEDED
    assert manager.download_artifact(consumer_id, "result.txt") == b"optional-ok"
    assert any(event.message == "artifact input skipped" for event in events)


def test_create_task_rejects_unknown_dependency(tmp_path: Path) -> None:
    manager = JobManager(root=tmp_path, runtime=WorkflowRuntime())

    with pytest.raises(JobNotFoundError):
        manager.create_task(
            AgentSpec(name="consumer"),
            {"mode": "consume"},
            dependencies=["missing"],
        )
