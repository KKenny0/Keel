"""High-level job management API."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from keel_runtime.errors import StorageSyncError
from keel_runtime.events import EventType, JobEvent, utc_now
from keel_runtime.jobs import AgentJob, JobStatus
from keel_runtime.object_storage import ObjectStorage
from keel_runtime.runtime import AgentRuntime, PiRpcRuntime, resolve_store_path
from keel_runtime.specs import AgentSpec
from keel_runtime.stores import LocalStores


class JobManager:
    def __init__(
        self,
        root: str | Path | None = None,
        runtime: AgentRuntime | None = None,
        object_storage: ObjectStorage | None = None,
    ) -> None:
        self.root = resolve_store_path(root)
        self.stores = LocalStores(self.root)
        self.runtime = runtime or PiRpcRuntime()
        self.object_storage = object_storage
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._queues: dict[str, asyncio.Queue[JobEvent | None]] = {}
        self._stop_requested: set[str] = set()
        self._mark_unfinished_jobs_restorable()

    def create_job(
        self,
        spec: AgentSpec,
        input: Any,
        workspace: str | Path | None = None,
    ) -> str:
        job_id = uuid.uuid4().hex
        self.stores.ensure_job(job_id)
        workspace_path = self.stores.workspaces.create(job_id, workspace)
        now = utc_now()
        job = AgentJob(
            id=job_id,
            spec=spec,
            input=input,
            status=JobStatus.CREATED,
            session_path=str(self.stores.layout.session_dir(job_id)),
            workspace_path=str(workspace_path),
            artifact_path=str(self.stores.layout.artifact_dir(job_id)),
            created_at=now,
            updated_at=now,
        )
        self.stores.jobs.save(job)
        self._append_event(JobEvent.status(job_id, "job created", status=JobStatus.CREATED.value))
        return job_id

    async def stream(self, job_id: str) -> AsyncIterator[JobEvent]:
        job = self.stores.jobs.load(job_id)
        if job.status.is_terminal:
            for event in self.stores.sessions.read(job_id):
                yield event
            return

        queue = self._queues.get(job_id)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[job_id] = queue

        if job_id not in self._tasks:
            self._tasks[job_id] = asyncio.create_task(self._run(job_id))

        while True:
            event = await queue.get()
            if event is None:
                break
            yield event

    async def resume(self, job_id: str) -> AsyncIterator[JobEvent]:
        job = self.stores.jobs.load(job_id)
        if job.status == JobStatus.SUCCEEDED:
            for event in self.stores.sessions.read(job_id):
                yield event
            return
        job.with_status(JobStatus.CREATED, error=None)
        self.stores.jobs.save(job)
        self._append_event(JobEvent.status(job_id, "job resumed", status=JobStatus.CREATED.value))
        async for event in self.stream(job_id):
            yield event

    async def stop(self, job_id: str) -> JobStatus:
        job = self.stores.jobs.load(job_id)
        if job.status.is_terminal:
            return job.status

        self._stop_requested.add(job_id)
        job.with_status(JobStatus.STOPPING)
        self.stores.jobs.save(job)
        event = JobEvent.status(job_id, "job stopping", status=JobStatus.STOPPING.value)
        self._append_event(event)
        await self._publish(job_id, event)

        if job_id not in self._tasks:
            job.with_status(JobStatus.STOPPED)
            self.stores.jobs.save(job)
            stopped_event = JobEvent.status(job_id, "job stopped", status=JobStatus.STOPPED.value)
            self._append_event(stopped_event)
            await self._publish(job_id, stopped_event)
            await self._finish_queue(job_id)
            self._stop_requested.discard(job_id)
            return JobStatus.STOPPED

        await self.runtime.stop(job_id)
        return JobStatus.STOPPING

    def get_status(self, job_id: str) -> JobStatus:
        return self.stores.jobs.load(job_id).status

    def get_job(self, job_id: str) -> AgentJob:
        return self.stores.jobs.load(job_id)

    def list_jobs(self) -> list[AgentJob]:
        return self.stores.jobs.list()

    def list_artifacts(self, job_id: str) -> list[str]:
        self.stores.jobs.load(job_id)
        return self.stores.artifacts.list(job_id)

    def download_artifact(self, job_id: str, path: str) -> bytes:
        self.stores.jobs.load(job_id)
        return self.stores.artifacts.read_bytes(job_id, path)

    def read_session(self, job_id: str) -> list[JobEvent]:
        self.stores.jobs.load(job_id)
        return self.stores.sessions.read(job_id)

    def snapshot_job(self, job_id: str) -> dict[str, Any]:
        return self.stores.build_record(job_id)

    def export_job(self, job_id: str) -> Path:
        return self.stores.export_job(job_id)

    def sync_job(self, job_id: str) -> list[str]:
        if self.object_storage is None:
            raise StorageSyncError("object storage is not configured")
        try:
            return self.stores.sync_to_object_storage(job_id, self.object_storage)
        except Exception as exc:
            raise StorageSyncError(f"object storage sync failed: {exc}") from exc

    def restore_job(self, job_id: str) -> AgentJob:
        if self.object_storage is None:
            raise StorageSyncError("object storage is not configured")
        try:
            return self.stores.restore_from_object_storage(job_id, self.object_storage)
        except Exception as exc:
            raise StorageSyncError(f"object storage restore failed: {exc}") from exc

    async def _run(self, job_id: str) -> None:
        output: list[str] = []
        job = self.stores.jobs.load(job_id)
        job.with_status(JobStatus.RUNNING)
        self.stores.jobs.save(job)
        await self._record_and_publish(
            JobEvent.status(job_id, "job running", status=job.status.value)
        )

        try:
            async for event in self.runtime.run(job, job.spec):
                if event.type == EventType.OUTPUT:
                    output.append(event.message)
                await self._record_and_publish(event)
            result = "\n".join(output)
            if result:
                self.stores.artifacts.write_text(job_id, "result.txt", result)
                await self._record_and_publish(
                    JobEvent(
                        job_id=job_id,
                        type=EventType.ARTIFACT,
                        message="artifact written",
                        data={"path": "result.txt"},
                    )
                )

            final_status = (
                JobStatus.STOPPED if job_id in self._stop_requested else JobStatus.SUCCEEDED
            )
            job = self.stores.jobs.load(job_id)
            job.with_status(final_status, result=result)
            self.stores.jobs.save(job)
            await self._record_and_publish(
                JobEvent.status(job_id, f"job {final_status.value}", status=final_status.value)
            )
            if self.object_storage is not None:
                uploaded_keys = self.sync_job(job_id)
                await self._record_and_publish(
                    JobEvent.status(
                        job_id,
                        "job synced",
                        object_count=len(uploaded_keys),
                    )
                )
        except Exception as exc:
            job = self.stores.jobs.load(job_id)
            job.with_status(JobStatus.FAILED, error=str(exc))
            self.stores.jobs.save(job)
            await self._record_and_publish(
                JobEvent.error(job_id, str(exc), status=JobStatus.FAILED.value)
            )
        finally:
            self._stop_requested.discard(job_id)
            await self._finish_queue(job_id)
            self._tasks.pop(job_id, None)
            self._queues.pop(job_id, None)

    def _mark_unfinished_jobs_restorable(self) -> None:
        for job in self.stores.unfinished_jobs():
            if job.status in {JobStatus.RUNNING, JobStatus.STOPPING}:
                job.with_status(JobStatus.RESTORABLE)
                self.stores.jobs.save(job)
                self._append_event(
                    JobEvent.status(
                        job.id,
                        "job marked restorable",
                        status=JobStatus.RESTORABLE.value,
                    )
                )

    def _append_event(self, event: JobEvent) -> None:
        self.stores.sessions.append(event)

    async def _record_and_publish(self, event: JobEvent) -> None:
        self._append_event(event)
        await self._publish(event.job_id, event)

    async def _publish(self, job_id: str, event: JobEvent) -> None:
        queue = self._queues.get(job_id)
        if queue is not None:
            await queue.put(event)

    async def _finish_queue(self, job_id: str) -> None:
        queue = self._queues.get(job_id)
        if queue is not None:
            await queue.put(None)
