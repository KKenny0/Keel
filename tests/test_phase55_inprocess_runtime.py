from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from keel_runtime import AgentSpec, EventType, InProcessRuntime, JobManager, JobStatus


def collect(async_iter):
    async def _collect():
        return [event async for event in async_iter]

    return asyncio.run(_collect())


def test_inprocess_runtime_runs_registered_async_callable(tmp_path: Path) -> None:
    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        workspace = Path(payload["workspace_path"])
        (workspace / "seen.txt").write_text(payload["job_id"], encoding="utf-8")
        return {"message": payload["input"]["message"], "agent": payload["agent"]["name"]}

    manager = JobManager(
        root=tmp_path / "data",
        runtime=InProcessRuntime({"worker": handler}),
    )
    job_id = manager.create_job(AgentSpec(name="worker"), {"message": "hello"})

    events = collect(manager.stream(job_id))

    job = manager.get_job(job_id)
    result = manager.download_artifact(job_id, "result.txt").decode("utf-8")
    assert job.status == JobStatus.SUCCEEDED
    assert '"message": "hello"' in result
    assert '"agent": "worker"' in result
    assert (Path(job.workspace_path) / "seen.txt").read_text(encoding="utf-8") == job_id
    assert any(
        event.type == EventType.STATUS and event.message == "in-process callable started"
        for event in events
    )
    assert any(
        event.type == EventType.OUTPUT and event.data.get("stream") == "inprocess"
        for event in events
    )


def test_inprocess_runtime_default_handler_can_run_sync_callable(tmp_path: Path) -> None:
    def handler(payload: dict[str, Any]) -> str:
        return f"default:{payload['input']['value']}"

    manager = JobManager(root=tmp_path / "data", runtime=InProcessRuntime(default=handler))
    job_id = manager.create_job(AgentSpec(name="any-agent"), {"value": "ok"})

    collect(manager.stream(job_id))

    assert manager.get_status(job_id) == JobStatus.SUCCEEDED
    assert manager.download_artifact(job_id, "result.txt") == b"default:ok"


def test_inprocess_runtime_callable_exception_marks_job_failed(tmp_path: Path) -> None:
    async def handler(payload: dict[str, Any]) -> str:
        raise ValueError(f"bad input: {payload['input']['message']}")

    manager = JobManager(root=tmp_path / "data", runtime=InProcessRuntime({"boom": handler}))
    job_id = manager.create_job(AgentSpec(name="boom"), {"message": "explode"})

    events = collect(manager.stream(job_id))

    job = manager.get_job(job_id)
    assert job.status == JobStatus.FAILED
    assert "bad input: explode" in (job.error or "")
    assert any(event.type == EventType.ERROR for event in events)


def test_inprocess_runtime_missing_handler_marks_job_failed(tmp_path: Path) -> None:
    manager = JobManager(root=tmp_path / "data", runtime=InProcessRuntime())
    job_id = manager.create_job(AgentSpec(name="missing"), {"message": "no handler"})

    collect(manager.stream(job_id))

    job = manager.get_job(job_id)
    assert job.status == JobStatus.FAILED
    assert "not registered" in (job.error or "")


def test_inprocess_runtime_stop_cancels_running_callable(tmp_path: Path) -> None:
    async def scenario() -> tuple[JobManager, str, list[str]]:
        markers: list[str] = []
        started = asyncio.Event()

        async def handler(payload: dict[str, Any]) -> str:
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                markers.append("cancelled")
            return "too late"

        manager = JobManager(root=tmp_path / "data", runtime=InProcessRuntime({"slow": handler}))
        job_id = manager.create_job(AgentSpec(name="slow"), {"message": "stop"})

        async def consume() -> None:
            async for event in manager.stream(job_id):
                if event.message == "in-process callable started":
                    await started.wait()
                    await manager.stop(job_id)

        await asyncio.wait_for(consume(), timeout=2)
        return manager, job_id, markers

    manager, job_id, markers = asyncio.run(scenario())

    assert manager.get_status(job_id) == JobStatus.STOPPED
    assert markers == ["cancelled"]
    assert any(event.message == "job stopped" for event in manager.read_session(job_id))


def test_inprocess_runtime_timeout_marks_job_failed(tmp_path: Path) -> None:
    async def handler(payload: dict[str, Any]) -> str:
        await asyncio.sleep(60)
        return "too late"

    manager = JobManager(root=tmp_path / "data", runtime=InProcessRuntime({"slow": handler}))
    job_id = manager.create_job(
        AgentSpec(name="slow", timeout_seconds=0.05),
        {"message": "timeout"},
    )

    events = collect(manager.stream(job_id))

    job = manager.get_job(job_id)
    assert job.status == JobStatus.FAILED
    assert job.timed_out is True
    assert "timed out" in (job.error or "")
    assert any(event.type == EventType.ERROR and event.data["timed_out"] for event in events)

