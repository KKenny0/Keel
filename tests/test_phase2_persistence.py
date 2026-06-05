from __future__ import annotations

import asyncio
import json
import zipfile
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path

from keel_runtime import (
    AgentSpec,
    InMemoryObjectStorage,
    JobEvent,
    JobManager,
    JobStatus,
    S3ObjectStorage,
)


def collect(async_iter):
    async def _collect():
        return [event async for event in async_iter]

    return asyncio.run(_collect())


class WorkspaceRuntime:
    async def run(self, job, spec) -> AsyncIterator[JobEvent]:
        workspace = Path(job.workspace_path)
        run_log = workspace / "run-log.txt"
        previous = run_log.read_text(encoding="utf-8") if run_log.exists() else ""
        run_log.write_text(previous + f"run:{job.input['message']}\n", encoding="utf-8")
        yield JobEvent.output(job.id, f"output:{job.input['message']}")

    async def stop(self, job_id: str) -> None:
        return None


class FailThenRecoverRuntime:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, job, spec) -> AsyncIterator[JobEvent]:
        self.calls += 1
        workspace = Path(job.workspace_path)
        marker = workspace / "resume-marker.txt"
        previous = marker.read_text(encoding="utf-8") if marker.exists() else ""
        marker.write_text(previous + f"call:{self.calls}\n", encoding="utf-8")
        if self.calls == 1:
            raise RuntimeError("first run failed")
        yield JobEvent.output(job.id, "recovered")

    async def stop(self, job_id: str) -> None:
        return None


class FailingObjectStorage:
    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        raise RuntimeError("object store down")

    def get_bytes(self, key: str) -> bytes:
        raise FileNotFoundError(key)

    def list_keys(self, prefix: str) -> list[str]:
        return []


def test_resume_failed_job_preserves_workspace(tmp_path: Path) -> None:
    runtime = FailThenRecoverRuntime()
    manager = JobManager(root=tmp_path, runtime=runtime)
    job_id = manager.create_job(AgentSpec(name="recover"), {"message": "resume"})

    collect(manager.stream(job_id))
    assert manager.get_status(job_id) == JobStatus.FAILED

    events = collect(manager.resume(job_id))

    assert manager.get_status(job_id) == JobStatus.SUCCEEDED
    assert any(event.message == "job resumed" for event in manager.read_session(job_id))
    assert any(event.message == "recovered" for event in events)
    marker = Path(manager.get_job(job_id).workspace_path) / "resume-marker.txt"
    assert marker.read_text(encoding="utf-8") == "call:1\ncall:2\n"


def test_export_job_contains_record_session_workspace_and_artifacts(tmp_path: Path) -> None:
    manager = JobManager(root=tmp_path, runtime=WorkspaceRuntime())
    job_id = manager.create_job(AgentSpec(name="writer"), {"message": "export"})
    collect(manager.stream(job_id))

    export_path = manager.export_job(job_id)

    assert export_path.exists()
    assert manager.download_artifact(job_id, "job-record.json")
    with zipfile.ZipFile(export_path) as archive:
        names = set(archive.namelist())
        assert "record.json" in names
        assert "job/job.json" in names
        assert "session/events.jsonl" in names
        assert "workspace/run-log.txt" in names
        assert "artifacts/result.txt" in names
        record = json.loads(archive.read("record.json"))
    assert record["job"]["id"] == job_id
    assert record["workspace"][0]["path"] == "run-log.txt"
    assert record["workspace"][0]["size"] > 0


def test_object_storage_sync_uploads_separate_job_parts(tmp_path: Path) -> None:
    storage = InMemoryObjectStorage()
    manager = JobManager(root=tmp_path, runtime=WorkspaceRuntime(), object_storage=storage)
    job_id = manager.create_job(AgentSpec(name="writer"), {"message": "sync"})
    collect(manager.stream(job_id))

    keys = set(storage.objects)

    assert f"jobs/{job_id}/record.json" in keys
    assert f"jobs/{job_id}/job/job.json" in keys
    assert f"jobs/{job_id}/session/events.jsonl" in keys
    assert f"jobs/{job_id}/workspace/run-log.txt" in keys
    assert f"jobs/{job_id}/artifacts/result.txt" in keys


def test_object_storage_failure_marks_job_failed(tmp_path: Path) -> None:
    manager = JobManager(
        root=tmp_path,
        runtime=WorkspaceRuntime(),
        object_storage=FailingObjectStorage(),
    )
    job_id = manager.create_job(AgentSpec(name="writer"), {"message": "fail-sync"})

    events = collect(manager.stream(job_id))

    job = manager.get_job(job_id)
    assert job.status == JobStatus.FAILED
    assert "object storage sync failed" in (job.error or "")
    assert any(event.type.value == "error" for event in events)


def test_restore_job_from_object_storage_to_new_root(tmp_path: Path) -> None:
    storage = InMemoryObjectStorage()
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = JobManager(root=first_root, runtime=WorkspaceRuntime(), object_storage=storage)
    job_id = first.create_job(AgentSpec(name="writer"), {"message": "restore"})
    collect(first.stream(job_id))

    second = JobManager(root=second_root, runtime=WorkspaceRuntime(), object_storage=storage)
    restored = second.restore_job(job_id)

    assert restored.id == job_id
    assert second.get_status(job_id) == JobStatus.SUCCEEDED
    assert second.download_artifact(job_id, "result.txt") == b"output:restore"
    workspace_file = Path(second.get_job(job_id).workspace_path) / "run-log.txt"
    assert workspace_file.read_text(encoding="utf-8") == "run:restore\n"
    assert any(event.message == "output:restore" for event in second.read_session(job_id))


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        return {"Body": BytesIO(self.objects[(Bucket, Key)])}

    def list_objects_v2(self, Bucket, Prefix):
        return {
            "Contents": [
                {"Key": key}
                for bucket, key in self.objects
                if bucket == Bucket and key.startswith(Prefix)
            ]
        }


def test_s3_object_storage_uses_bucket_prefix_and_client() -> None:
    client = FakeS3Client()
    storage = S3ObjectStorage(bucket="keel", prefix="project-a", client=client)

    storage.put_bytes("jobs/1/record.json", b"{}", "application/json")

    assert storage.get_bytes("jobs/1/record.json") == b"{}"
    assert storage.list_keys("jobs/1") == ["jobs/1/record.json"]
    assert ("keel", "project-a/jobs/1/record.json") in client.objects
