from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from keel_runtime import (
    AgentSpec,
    ArtifactInput,
    CollaborationStatus,
    CollaborationStepStatus,
    EventType,
    JobAttemptKind,
    JobEvent,
    JobManager,
    JobStatus,
)
from keel_runtime.errors import InvalidJobStateError


def collect(async_iter):
    async def _collect():
        return [event async for event in async_iter]

    return asyncio.run(_collect())


class CollaborationRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.flaky_failed = False
        self.started: list[str] = []

    async def run(self, job, spec) -> AsyncIterator[JobEvent]:
        payload = job.input
        mode = payload["input"]["mode"]
        workspace = Path(job.workspace_path)
        self.calls.append((spec.name, mode))
        self.started.append(mode)

        if mode == "analyze":
            source = workspace / "repo.py"
            yield JobEvent.output(job.id, f"analysis:{source.read_text(encoding='utf-8')}")
            return

        if mode == "edit":
            analysis = (workspace / "inputs" / "analysis.txt").read_text(encoding="utf-8")
            (workspace / "patch.diff").write_text(f"patch from {analysis}", encoding="utf-8")
            yield JobEvent.output(job.id, f"edit:{analysis}")
            return

        if mode == "report":
            yield JobEvent.output(job.id, f"report:{payload['collaboration']['context']['branch']}")
            return

        if mode == "parallel":
            yield JobEvent.output(job.id, f"parallel:{spec.name}")
            return

        if mode == "flaky":
            if not self.flaky_failed:
                self.flaky_failed = True
                (workspace / "dirty.txt").write_text("failed attempt", encoding="utf-8")
                raise RuntimeError("agent crashed")
            yield JobEvent.output(job.id, f"retry-clean={not (workspace / 'dirty.txt').exists()}")
            return

        raise AssertionError(f"unknown mode: {mode}")

    async def stop(self, job_id: str) -> None:
        return None


def test_collaboration_runs_multiple_agents_with_shared_project_and_artifacts(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "repo.py").write_text("print('keel')", encoding="utf-8")
    manager = JobManager(root=tmp_path / "data", runtime=CollaborationRuntime())

    collaboration_id = manager.create_collaboration(
        "Improve repository",
        workspace=project,
        context={"branch": "main"},
    )
    analysis_step_id = manager.add_collaboration_step(
        collaboration_id,
        AgentSpec(name="analyst"),
        {"mode": "analyze"},
    )
    analysis_job_id = manager.get_collaboration_step(
        collaboration_id,
        analysis_step_id,
    ).job_id
    assert analysis_job_id is not None
    collect(manager.stream_collaboration_step(collaboration_id, analysis_step_id))

    edit_step_id = manager.add_collaboration_step(
        collaboration_id,
        AgentSpec(name="editor"),
        {"mode": "edit"},
        dependencies=[analysis_job_id],
        artifact_inputs=[
            ArtifactInput(
                source_job_id=analysis_job_id,
                source_path="result.txt",
                target_path="inputs/analysis.txt",
            )
        ],
    )
    edit_events = collect(manager.stream_collaboration_step(collaboration_id, edit_step_id))

    collaboration = manager.get_collaboration(collaboration_id)
    analysis_job = manager.get_job(analysis_job_id)
    edit_job_id = collaboration.steps[1].job_id
    assert edit_job_id is not None
    edit_job = manager.get_job(edit_job_id)
    assert collaboration.status == CollaborationStatus.SUCCEEDED
    assert collaboration.steps[0].status == CollaborationStepStatus.SUCCEEDED
    assert collaboration.steps[1].status == CollaborationStepStatus.SUCCEEDED
    assert Path(analysis_job.workspace_path) != Path(edit_job.workspace_path)
    assert (Path(edit_job.workspace_path) / "repo.py").exists()
    assert (Path(edit_job.workspace_path) / "inputs" / "analysis.txt").exists()
    assert any(
        event.type == EventType.OUTPUT and event.message.startswith("edit:analysis:")
        for event in edit_events
    )

    description = manager.describe_collaboration(collaboration_id)
    assert [step["agent_name"] for step in description["steps"]] == ["analyst", "editor"]
    assert description["steps"][1]["attempts"][0]["artifacts"] == ["result.txt"]
    assert any(
        event["message"] == "collaboration step attached"
        for event in description["steps"][0]["attempts"][0]["events"]
    )


def test_collaboration_confirmation_gates_step_until_approved(tmp_path: Path) -> None:
    manager = JobManager(root=tmp_path, runtime=CollaborationRuntime())
    collaboration_id = manager.create_collaboration("Review before write")
    step_id = manager.add_collaboration_step(
        collaboration_id,
        AgentSpec(name="reporter"),
        {"mode": "report"},
        requires_confirmation=True,
        context={"branch": "release"},
    )

    step = manager.get_collaboration_step(collaboration_id, step_id)
    assert step.status == CollaborationStepStatus.WAITING_FOR_CONFIRMATION
    assert step.job_id is None
    assert manager.get_collaboration(collaboration_id).status == (
        CollaborationStatus.WAITING_FOR_CONFIRMATION
    )
    with pytest.raises(InvalidJobStateError):
        collect(manager.stream_collaboration_step(collaboration_id, step_id))

    job_id = manager.confirm_collaboration_step(
        collaboration_id,
        step_id,
        note="approved",
    )
    events = collect(manager.stream_collaboration_step(collaboration_id, step_id))

    assert manager.get_status(job_id) == JobStatus.SUCCEEDED
    assert manager.get_collaboration_step(collaboration_id, step_id).confirmation_note == "approved"
    assert any(event.message == "report:release" for event in events)


def test_collaboration_can_run_parallel_steps(tmp_path: Path) -> None:
    runtime = CollaborationRuntime()
    manager = JobManager(root=tmp_path, runtime=runtime)
    collaboration_id = manager.create_collaboration("Parallel scan")
    first_step_id = manager.add_collaboration_step(
        collaboration_id,
        AgentSpec(name="lint-agent"),
        {"mode": "parallel"},
    )
    second_step_id = manager.add_collaboration_step(
        collaboration_id,
        AgentSpec(name="test-agent"),
        {"mode": "parallel"},
    )

    async def run_parallel() -> None:
        await asyncio.gather(
            collect_async(manager.stream_collaboration_step(collaboration_id, first_step_id)),
            collect_async(manager.stream_collaboration_step(collaboration_id, second_step_id)),
        )

    async def collect_async(async_iter) -> list[JobEvent]:
        return [event async for event in async_iter]

    asyncio.run(run_parallel())

    assert sorted(runtime.started) == ["parallel", "parallel"]
    assert manager.get_collaboration(collaboration_id).status == CollaborationStatus.SUCCEEDED


def test_failed_collaboration_step_can_retry_from_clean_project_workspace(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "repo.py").write_text("clean", encoding="utf-8")
    manager = JobManager(root=tmp_path / "data", runtime=CollaborationRuntime())
    collaboration_id = manager.create_collaboration("Retry safely", workspace=project)
    step_id = manager.add_collaboration_step(
        collaboration_id,
        AgentSpec(name="flaky-agent"),
        {"mode": "flaky"},
        max_attempts=2,
    )

    collect(manager.stream_collaboration_step(collaboration_id, step_id))
    failed_job_id = manager.get_collaboration_step(collaboration_id, step_id).job_id
    assert failed_job_id is not None
    assert manager.get_status(failed_job_id) == JobStatus.FAILED
    assert (Path(manager.get_job(failed_job_id).workspace_path) / "dirty.txt").exists()

    retry_job_id = manager.retry_collaboration_step(collaboration_id, step_id)
    retry_events = collect(manager.stream_collaboration_step(collaboration_id, step_id))

    step = manager.get_collaboration_step(collaboration_id, step_id)
    description = manager.describe_collaboration(collaboration_id)
    assert retry_job_id != failed_job_id
    assert manager.get_status(retry_job_id) == JobStatus.SUCCEEDED
    retry_attempt = manager.stores.attempts.load(retry_job_id, "attempt-1")
    assert retry_attempt.kind == JobAttemptKind.RETRY
    assert retry_attempt.retry_of == failed_job_id
    assert step.job_ids == [failed_job_id, retry_job_id]
    assert step.status == CollaborationStepStatus.SUCCEEDED
    assert [attempt["status"] for attempt in description["steps"][0]["attempts"]] == [
        "failed",
        "succeeded",
    ]
    assert any(event.message == "retry-clean=True" for event in retry_events)


def test_restorable_collaboration_step_survives_restart_and_resumes(tmp_path: Path) -> None:
    first = JobManager(root=tmp_path, runtime=CollaborationRuntime())
    collaboration_id = first.create_collaboration(
        "Resume midway",
        context={"branch": "resume"},
    )
    step_id = first.add_collaboration_step(
        collaboration_id,
        AgentSpec(name="reporter"),
        {"mode": "report"},
    )
    job_id = first.get_collaboration_step(collaboration_id, step_id).job_id
    assert job_id is not None
    job = first.get_job(job_id)
    job.with_status(JobStatus.RUNNING)
    first.stores.jobs.save(job)

    second = JobManager(root=tmp_path, runtime=CollaborationRuntime())
    collaboration = second.get_collaboration(collaboration_id)
    assert collaboration.status == CollaborationStatus.RESTORABLE
    assert collaboration.steps[0].status == CollaborationStepStatus.RESTORABLE

    with pytest.warns(DeprecationWarning):
        events = collect(second.resume_collaboration_step(collaboration_id, step_id))

    assert second.get_status(job_id) == JobStatus.SUCCEEDED
    assert second.get_collaboration(collaboration_id).status == CollaborationStatus.SUCCEEDED
    assert any(event.message == "report:resume" for event in events)
