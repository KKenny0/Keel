from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from keel_runtime import AgentSpec, EventType, JobEvent, JobManager, JobStatus, PiRpcRuntime


class WritingRuntime:
    async def run(self, job, spec) -> AsyncIterator[JobEvent]:
        workspace = Path(job.workspace_path)
        (workspace / "answer.txt").write_text(f"job={job.id}", encoding="utf-8")
        yield JobEvent.output(job.id, f"hello {job.input['message']}")
        yield JobEvent.log(job.id, "done")

    async def stop(self, job_id: str) -> None:
        return None


class StoppableRuntime:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def run(self, job, spec) -> AsyncIterator[JobEvent]:
        self.started.set()
        yield JobEvent.output(job.id, "before stop")
        await self.stopped.wait()
        yield JobEvent.output(job.id, "after stop")

    async def stop(self, job_id: str) -> None:
        self.stopped.set()


def collect(async_iter):
    async def _collect():
        return [event async for event in async_iter]

    return asyncio.run(_collect())


def test_create_stream_complete_and_persist_artifacts(tmp_path: Path) -> None:
    manager = JobManager(root=tmp_path, runtime=WritingRuntime())
    job_id = manager.create_job(AgentSpec(name="writer"), {"message": "keel"})

    events = collect(manager.stream(job_id))

    assert manager.get_status(job_id) == JobStatus.SUCCEEDED
    assert any(event.type == EventType.OUTPUT and event.message == "hello keel" for event in events)
    assert manager.download_artifact(job_id, "result.txt") == b"hello keel"
    assert (Path(manager.get_job(job_id).workspace_path) / "answer.txt").exists()
    assert len(manager.read_session(job_id)) >= 4


def test_stop_job_preserves_session_and_workspace(tmp_path: Path) -> None:
    async def scenario() -> tuple[JobStatus, list[JobEvent], JobManager, str]:
        runtime = StoppableRuntime()
        manager = JobManager(root=tmp_path, runtime=runtime)
        job_id = manager.create_job(AgentSpec(name="slow"), {"message": "stop"})
        collected: list[JobEvent] = []

        async def consume() -> None:
            async for event in manager.stream(job_id):
                collected.append(event)
                if event.type == EventType.OUTPUT and event.message == "before stop":
                    await manager.stop(job_id)

        await asyncio.wait_for(consume(), timeout=2)
        return manager.get_status(job_id), collected, manager, job_id

    status, events, manager, job_id = asyncio.run(scenario())

    assert status == JobStatus.STOPPED
    assert any(event.message == "job stopping" for event in events)
    assert Path(manager.get_job(job_id).workspace_path).exists()
    assert manager.read_session(job_id)


def test_restart_can_query_historical_job(tmp_path: Path) -> None:
    first = JobManager(root=tmp_path, runtime=WritingRuntime())
    job_id = first.create_job(AgentSpec(name="writer"), {"message": "history"})
    collect(first.stream(job_id))

    second = JobManager(root=tmp_path, runtime=WritingRuntime())

    assert second.get_status(job_id) == JobStatus.SUCCEEDED
    assert second.list_artifacts(job_id) == ["result.txt"]
    assert any(event.message == "hello history" for event in second.read_session(job_id))


def test_running_jobs_are_restorable_after_manager_restarts(tmp_path: Path) -> None:
    first = JobManager(root=tmp_path, runtime=WritingRuntime())
    job_id = first.create_job(AgentSpec(name="writer"), {"message": "restore"})
    job = first.get_job(job_id)
    job.with_status(JobStatus.RUNNING)
    first.stores.jobs.save(job)

    second = JobManager(root=tmp_path, runtime=WritingRuntime())

    assert second.get_status(job_id) == JobStatus.RESTORABLE


def test_jobs_get_isolated_workspaces(tmp_path: Path) -> None:
    manager = JobManager(root=tmp_path, runtime=WritingRuntime())
    first_id = manager.create_job(AgentSpec(name="writer"), {"message": "one"})
    second_id = manager.create_job(AgentSpec(name="writer"), {"message": "two"})

    collect(manager.stream(first_id))
    collect(manager.stream(second_id))

    first_workspace = Path(manager.get_job(first_id).workspace_path)
    second_workspace = Path(manager.get_job(second_id).workspace_path)
    assert first_workspace != second_workspace
    assert (first_workspace / "answer.txt").read_text(encoding="utf-8") == f"job={first_id}"
    assert (second_workspace / "answer.txt").read_text(encoding="utf-8") == f"job={second_id}"


def test_pi_rpc_runtime_streams_local_process_output(tmp_path: Path) -> None:
    runner = tmp_path / "fake_pi_rpc.py"
    runner.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                "payload = json.load(sys.stdin)",
                "workspace = pathlib.Path(payload['workspace_path'])",
                "workspace.joinpath('from-runtime.txt').write_text(",
                "    payload['job_id'], encoding='utf-8'",
                ")",
                "print('stdout:' + payload['input']['message'], flush=True)",
                "print('stderr:ok', file=sys.stderr, flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    manager = JobManager(
        root=tmp_path / "data",
        runtime=PiRpcRuntime([sys.executable, str(runner)]),
    )
    job_id = manager.create_job(AgentSpec(name="fake-pi"), {"message": "rpc"})

    events = collect(manager.stream(job_id))

    assert manager.get_status(job_id) == JobStatus.SUCCEEDED
    assert any(event.type == EventType.OUTPUT and event.message == "stdout:rpc" for event in events)
    assert any(event.type == EventType.LOG and event.message == "stderr:ok" for event in events)
    assert (Path(manager.get_job(job_id).workspace_path) / "from-runtime.txt").exists()


def test_stop_created_job_marks_it_stopped(tmp_path: Path) -> None:
    async def scenario() -> JobStatus:
        manager = JobManager(root=tmp_path, runtime=WritingRuntime())
        job_id = manager.create_job(AgentSpec(name="idle"), {"message": "idle"})
        status = await manager.stop(job_id)
        assert manager.get_status(job_id) == JobStatus.STOPPED
        assert any(event.message == "job stopped" for event in manager.read_session(job_id))
        return status

    assert asyncio.run(scenario()) == JobStatus.STOPPED
