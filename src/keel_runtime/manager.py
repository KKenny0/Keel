"""High-level job management API."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from keel_runtime.cleanup import CleanupPolicy
from keel_runtime.collaboration import (
    Collaboration,
    CollaborationStatus,
    CollaborationStep,
    CollaborationStepStatus,
)
from keel_runtime.errors import (
    DependencyError,
    InvalidJobStateError,
    RuntimeExecutionError,
    StorageSyncError,
)
from keel_runtime.events import EventType, JobEvent, utc_now
from keel_runtime.jobs import AgentJob, ArtifactInput, JobStatus
from keel_runtime.models import ModelConfig, ProviderRegistry, parse_model_usage
from keel_runtime.object_storage import ObjectStorage
from keel_runtime.runtime import AgentRuntime, PiRpcRuntime, resolve_store_path
from keel_runtime.security import redact_data, redact_text
from keel_runtime.specs import AgentSpec
from keel_runtime.stores import LocalStores


class JobManager:
    def __init__(
        self,
        root: str | Path | None = None,
        runtime: AgentRuntime | None = None,
        object_storage: ObjectStorage | None = None,
        cleanup_policy: CleanupPolicy | None = None,
        provider_registry: ProviderRegistry | None = None,
    ) -> None:
        self.root = resolve_store_path(root)
        self.stores = LocalStores(self.root)
        self.runtime = runtime or PiRpcRuntime()
        self.object_storage = object_storage
        self.cleanup_policy = cleanup_policy or CleanupPolicy()
        self.provider_registry = provider_registry or ProviderRegistry()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._queues: dict[str, asyncio.Queue[JobEvent | None]] = {}
        self._stop_requested: set[str] = set()
        self._runtime_specs: dict[str, AgentSpec] = {}
        self._secret_values: dict[str, list[str]] = {}
        self._mark_unfinished_jobs_restorable()
        self._sync_all_collaborations()

    def create_job(
        self,
        spec: AgentSpec,
        input: Any,
        workspace: str | Path | None = None,
        dependencies: list[str] | None = None,
        artifact_inputs: list[ArtifactInput | dict[str, Any]] | None = None,
    ) -> str:
        job_id = uuid.uuid4().hex
        normalized_artifact_inputs = self._normalize_artifact_inputs(artifact_inputs)
        normalized_dependencies = self._normalize_dependencies(
            dependencies,
            normalized_artifact_inputs,
        )
        self._runtime_specs[job_id] = spec
        self._secret_values[job_id] = spec.secret_values()
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
            dependencies=normalized_dependencies,
            artifact_inputs=normalized_artifact_inputs,
        )
        self.stores.jobs.save(job)
        self._append_event(JobEvent.status(job_id, "job created", status=JobStatus.CREATED.value))
        self._record_model_config_warnings(job_id, spec)
        return job_id

    def create_task(
        self,
        spec: AgentSpec,
        input: Any,
        workspace: str | Path | None = None,
        dependencies: list[str] | None = None,
        artifact_inputs: list[ArtifactInput | dict[str, Any]] | None = None,
    ) -> str:
        return self.create_job(
            spec,
            input,
            workspace,
            dependencies=dependencies,
            artifact_inputs=artifact_inputs,
        )

    def create_collaboration(
        self,
        goal: str,
        workspace: str | Path | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        collaboration_id = uuid.uuid4().hex
        workspace_path = self.stores.create_collaboration_workspace(collaboration_id, workspace)
        now = utc_now()
        collaboration = Collaboration(
            id=collaboration_id,
            goal=goal,
            status=CollaborationStatus.CREATED,
            project_workspace_path=str(workspace_path),
            context=dict(context or {}),
            steps=[],
            created_at=now,
            updated_at=now,
        )
        self.stores.collaborations.save(collaboration)
        return collaboration_id

    def list_collaborations(self) -> list[Collaboration]:
        collaborations: list[Collaboration] = []
        for collaboration in self.stores.collaborations.list():
            collaborations.append(self._sync_collaboration(collaboration))
        return collaborations

    def get_collaboration(self, collaboration_id: str) -> Collaboration:
        collaboration = self.stores.collaborations.load(collaboration_id)
        return self._sync_collaboration(collaboration)

    def describe_collaboration(self, collaboration_id: str) -> dict[str, Any]:
        collaboration = self.get_collaboration(collaboration_id)
        return {
            "collaboration": {
                "id": collaboration.id,
                "goal": collaboration.goal,
                "status": collaboration.status.value,
                "project_workspace_path": collaboration.project_workspace_path,
                "context": dict(collaboration.context),
                "created_at": collaboration.created_at.isoformat(),
                "updated_at": collaboration.updated_at.isoformat(),
            },
            "steps": [
                self._describe_collaboration_step(collaboration, step)
                for step in collaboration.steps
            ],
        }

    def update_collaboration_context(
        self,
        collaboration_id: str,
        context: dict[str, Any],
    ) -> Collaboration:
        collaboration = self.get_collaboration(collaboration_id)
        collaboration.context.update(context)
        collaboration.updated_at = utc_now()
        self.stores.collaborations.save(collaboration)
        return collaboration

    def add_collaboration_step(
        self,
        collaboration_id: str,
        spec: AgentSpec,
        input: Any,
        *,
        dependencies: list[str] | None = None,
        artifact_inputs: list[ArtifactInput | dict[str, Any]] | None = None,
        requires_confirmation: bool = False,
        max_attempts: int = 2,
        context: dict[str, Any] | None = None,
    ) -> str:
        collaboration = self.get_collaboration(collaboration_id)
        normalized_artifact_inputs = self._normalize_artifact_inputs(artifact_inputs)
        normalized_dependencies = self._normalize_dependencies(
            dependencies,
            normalized_artifact_inputs,
        )
        now = utc_now()
        step = CollaborationStep(
            id=uuid.uuid4().hex,
            agent_name=spec.name,
            spec=spec,
            input=input,
            status=(
                CollaborationStepStatus.WAITING_FOR_CONFIRMATION
                if requires_confirmation
                else CollaborationStepStatus.PENDING
            ),
            created_at=now,
            updated_at=now,
            dependencies=normalized_dependencies,
            artifact_inputs=normalized_artifact_inputs,
            requires_confirmation=requires_confirmation,
            max_attempts=max_attempts,
            context=dict(context or {}),
        )
        collaboration.steps.append(step)
        if not requires_confirmation:
            job_id = self._create_collaboration_job(collaboration, step)
            step.job_ids.append(job_id)
            step.with_status(CollaborationStepStatus.CREATED)
        self._save_synced_collaboration(collaboration)
        return step.id

    def get_collaboration_step(
        self,
        collaboration_id: str,
        step_id: str,
    ) -> CollaborationStep:
        collaboration = self.get_collaboration(collaboration_id)
        return self._find_collaboration_step(collaboration, step_id)

    def confirm_collaboration_step(
        self,
        collaboration_id: str,
        step_id: str,
        *,
        note: str | None = None,
    ) -> str:
        collaboration = self.get_collaboration(collaboration_id)
        step = self._find_collaboration_step(collaboration, step_id)
        if not step.requires_confirmation:
            raise InvalidJobStateError(
                f"Collaboration step does not require confirmation: {step_id}"
            )
        if step.job_id is not None:
            return step.job_id
        step.confirmed_at = utc_now()
        step.confirmation_note = note
        job_id = self._create_collaboration_job(collaboration, step)
        step.job_ids.append(job_id)
        step.with_status(CollaborationStepStatus.CREATED)
        self._append_event(
            JobEvent.status(
                job_id,
                "collaboration step confirmed",
                collaboration_id=collaboration.id,
                step_id=step.id,
                note=note,
            )
        )
        self._save_synced_collaboration(collaboration)
        return job_id

    async def stream_collaboration_step(
        self,
        collaboration_id: str,
        step_id: str,
    ) -> AsyncIterator[JobEvent]:
        job_id = self._collaboration_step_job_id(collaboration_id, step_id)
        async for event in self.stream(job_id):
            yield event
        self._sync_collaborations_for_job(job_id)

    async def resume_collaboration_step(
        self,
        collaboration_id: str,
        step_id: str,
    ) -> AsyncIterator[JobEvent]:
        job_id = self._collaboration_step_job_id(collaboration_id, step_id)
        async for event in self.resume(job_id):
            yield event
        self._sync_collaborations_for_job(job_id)

    def retry_collaboration_step(
        self,
        collaboration_id: str,
        step_id: str,
        *,
        force: bool = False,
    ) -> str:
        collaboration = self.get_collaboration(collaboration_id)
        step = self._find_collaboration_step(collaboration, step_id)
        latest_job_id = step.job_id
        if latest_job_id is None:
            raise InvalidJobStateError(f"Collaboration step has no job to retry: {step_id}")
        latest_job = self.stores.jobs.load(latest_job_id)
        retryable_statuses = {JobStatus.FAILED, JobStatus.STOPPED, JobStatus.RESTORABLE}
        if latest_job.status not in retryable_statuses and not force:
            raise InvalidJobStateError(
                f"Collaboration step is {latest_job.status.value}; retry is not allowed"
            )
        if step.attempt >= step.max_attempts:
            raise InvalidJobStateError(
                f"Collaboration step reached max attempts: {step.max_attempts}"
            )
        job_id = self._create_collaboration_job(collaboration, step)
        step.job_ids.append(job_id)
        step.with_status(CollaborationStepStatus.CREATED)
        self._append_event(
            JobEvent.status(
                job_id,
                "collaboration step retried",
                collaboration_id=collaboration.id,
                step_id=step.id,
                retry_of=latest_job_id,
                attempt=step.attempt,
            )
        )
        self._save_synced_collaboration(collaboration)
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
        job.with_status(JobStatus.CREATED, error=None, exit_code=None, timed_out=False)
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
            self._sync_collaborations_for_job(job_id)
            self._stop_requested.discard(job_id)
            self._runtime_specs.pop(job_id, None)
            self._secret_values.pop(job_id, None)
            return JobStatus.STOPPED

        await self.runtime.stop(job_id)
        return JobStatus.STOPPING

    def get_status(self, job_id: str) -> JobStatus:
        return self.stores.jobs.load(job_id).status

    def get_job(self, job_id: str) -> AgentJob:
        return self.stores.jobs.load(job_id)

    def list_jobs(self) -> list[AgentJob]:
        return self.stores.jobs.list()

    def describe_job(self, job_id: str) -> dict[str, Any]:
        job = self.stores.jobs.load(job_id)
        dependency_jobs = [
            self.stores.jobs.load(dependency_id)
            for dependency_id in job.dependencies or []
        ]
        return {
            "job": job.to_dict(),
            "status": job.status.value,
            "error": job.error,
            "exit_code": job.exit_code,
            "timed_out": job.timed_out,
            "dependencies": [
                {
                    "id": dependency.id,
                    "status": dependency.status.value,
                    "error": dependency.error,
                }
                for dependency in dependency_jobs
            ],
            "logs": [event.to_dict() for event in self.stores.sessions.read(job_id)],
            "artifacts": self.stores.artifacts.list(job_id),
        }

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

    def cleanup_job(
        self,
        job_id: str,
        *,
        remove_workspace: bool = True,
        remove_artifacts: bool = False,
    ) -> list[str]:
        return self.stores.cleanup_job(
            job_id,
            remove_workspace=remove_workspace,
            remove_artifacts=remove_artifacts,
        )

    async def _run(self, job_id: str) -> None:
        output: list[str] = []
        job = self.stores.jobs.load(job_id)
        spec = self._runtime_specs.get(job_id, job.spec)

        try:
            await self._prepare_dependencies(job_id)
            job = self.stores.jobs.load(job_id)
            job.with_status(JobStatus.RUNNING)
            self.stores.jobs.save(job)
            await self._record_and_publish(
                JobEvent.status(job_id, "job running", status=job.status.value)
            )
            async for event in self.runtime.run(job, spec):
                event = self._sanitize_event(event)
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
            job.with_status(final_status, result=result, exit_code=None, timed_out=False)
            await self._record_model_usage(job, output, scan_output=True)
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
            await self._apply_cleanup_policy(job_id, final_status)
        except Exception as exc:
            job = self.stores.jobs.load(job_id)
            await self._record_model_usage(job, [], scan_output=False)
            error = self._redact_text(job_id, str(exc))
            exit_code = exc.exit_code if isinstance(exc, RuntimeExecutionError) else None
            timed_out = exc.timed_out if isinstance(exc, RuntimeExecutionError) else False
            job.with_status(
                JobStatus.FAILED,
                error=error,
                exit_code=exit_code,
                timed_out=timed_out,
            )
            self.stores.jobs.save(job)
            await self._record_and_publish(
                JobEvent.error(
                    job_id,
                    error,
                    status=JobStatus.FAILED.value,
                    exit_code=exit_code,
                    timed_out=timed_out,
                )
            )
            await self._apply_cleanup_policy(job_id, JobStatus.FAILED)
        finally:
            self._stop_requested.discard(job_id)
            self._runtime_specs.pop(job_id, None)
            self._secret_values.pop(job_id, None)
            self._sync_collaborations_for_job(job_id)
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
        self.stores.sessions.append(self._sanitize_event(event))

    async def _record_and_publish(self, event: JobEvent) -> None:
        event = self._sanitize_event(event)
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

    async def _apply_cleanup_policy(self, job_id: str, status: JobStatus) -> None:
        remove_workspace = self.cleanup_policy.workspace_for_status(status)
        remove_artifacts = self.cleanup_policy.artifacts_for_status(status)
        if not remove_workspace and not remove_artifacts:
            return
        removed = self.cleanup_job(
            job_id,
            remove_workspace=remove_workspace,
            remove_artifacts=remove_artifacts,
        )
        if removed:
            await self._record_and_publish(
                JobEvent.status(job_id, "job cleaned", removed=removed)
            )

    def _sanitize_event(self, event: JobEvent) -> JobEvent:
        secrets = self._secret_values.get(event.job_id, [])
        if not secrets:
            return event
        return JobEvent(
            job_id=event.job_id,
            type=event.type,
            message=redact_text(event.message, secrets),
            data=redact_data(event.data, secrets),
            created_at=event.created_at,
        )

    def _redact_text(self, job_id: str, text: str) -> str:
        return redact_text(text, self._secret_values.get(job_id, []))

    def _record_model_config_warnings(self, job_id: str, spec: AgentSpec) -> None:
        if not isinstance(spec.model, ModelConfig):
            return
        warnings = self.provider_registry.validate(spec.model, secret_env=spec.secret_env)
        if warnings:
            self._append_event(
                JobEvent.status(job_id, "model config warnings", warnings=warnings)
            )

    async def _record_model_usage(
        self,
        job: AgentJob,
        output: list[str],
        *,
        scan_output: bool,
    ) -> None:
        usage, warnings = parse_model_usage(
            output,
            artifact_dir=job.artifact_path,
            scan_output=scan_output,
        )
        for warning in warnings:
            await self._record_and_publish(
                JobEvent.status(job.id, "model usage warning", warning=warning)
            )
        if usage is None:
            return
        job.model_usage = usage
        await self._record_and_publish(
            JobEvent.status(
                job.id,
                "model usage recorded",
                provider=usage.provider,
                model=usage.model,
                total_tokens=usage.total_tokens,
                cost_usd=usage.cost_usd,
            )
        )

    def _create_collaboration_job(
        self,
        collaboration: Collaboration,
        step: CollaborationStep,
    ) -> str:
        attempt = step.attempt + 1
        job_id = self.create_job(
            step.spec,
            self._collaboration_input(collaboration, step, attempt=attempt),
            workspace=collaboration.project_workspace_path,
            dependencies=step.dependencies,
            artifact_inputs=step.artifact_inputs,
        )
        self._append_event(
            JobEvent.status(
                job_id,
                "collaboration step attached",
                collaboration_id=collaboration.id,
                step_id=step.id,
                goal=collaboration.goal,
                agent_name=step.agent_name,
                attempt=attempt,
            )
        )
        return job_id

    def _collaboration_input(
        self,
        collaboration: Collaboration,
        step: CollaborationStep,
        *,
        attempt: int,
    ) -> dict[str, Any]:
        return {
            "input": step.input,
            "collaboration": {
                "id": collaboration.id,
                "goal": collaboration.goal,
                "step_id": step.id,
                "agent_name": step.agent_name,
                "attempt": attempt,
                "context": {**collaboration.context, **step.context},
                "dependencies": list(step.dependencies),
                "artifact_inputs": [
                    artifact_input.to_dict() for artifact_input in step.artifact_inputs
                ],
            },
        }

    def _collaboration_step_job_id(self, collaboration_id: str, step_id: str) -> str:
        collaboration = self.get_collaboration(collaboration_id)
        step = self._find_collaboration_step(collaboration, step_id)
        if step.job_id is None:
            raise InvalidJobStateError(
                f"Collaboration step is {step.status.value}; no job is available"
            )
        return step.job_id

    @staticmethod
    def _find_collaboration_step(
        collaboration: Collaboration,
        step_id: str,
    ) -> CollaborationStep:
        for step in collaboration.steps:
            if step.id == step_id:
                return step
        raise InvalidJobStateError(f"Collaboration step not found: {step_id}")

    def _describe_collaboration_step(
        self,
        collaboration: Collaboration,
        step: CollaborationStep,
    ) -> dict[str, Any]:
        description = step.to_dict()
        attempts: list[dict[str, Any]] = []
        for attempt_number, job_id in enumerate(step.job_ids, start=1):
            job = self.stores.jobs.load(job_id)
            attempts.append(
                {
                    "attempt": attempt_number,
                    "job_id": job.id,
                    "status": job.status.value,
                    "error": job.error,
                    "exit_code": job.exit_code,
                    "timed_out": job.timed_out,
                    "artifacts": self.stores.artifacts.list(job.id),
                    "events": [
                        event.to_dict() for event in self.stores.sessions.read(job.id)
                    ],
                }
            )
        description["collaboration_id"] = collaboration.id
        description["attempts"] = attempts
        return description

    def _save_synced_collaboration(self, collaboration: Collaboration) -> Collaboration:
        synced = self._sync_collaboration(collaboration, save=False)
        self.stores.collaborations.save(synced)
        return synced

    def _sync_all_collaborations(self) -> None:
        for collaboration in self.stores.collaborations.list():
            self._save_synced_collaboration(collaboration)

    def _sync_collaborations_for_job(self, job_id: str) -> None:
        for collaboration in self.stores.collaborations.list():
            if any(job_id in step.job_ids for step in collaboration.steps):
                self._save_synced_collaboration(collaboration)

    def _sync_collaboration(
        self,
        collaboration: Collaboration,
        *,
        save: bool = True,
    ) -> Collaboration:
        changed = False
        for step in collaboration.steps:
            changed = self._sync_collaboration_step(step) or changed
        status = self._derive_collaboration_status(collaboration.steps)
        if collaboration.status != status:
            collaboration.with_status(status)
            changed = True
        if changed and save:
            self.stores.collaborations.save(collaboration)
        return collaboration

    def _sync_collaboration_step(self, step: CollaborationStep) -> bool:
        status = self._expected_step_status(step)
        if status == step.status:
            return False
        step.with_status(status)
        return True

    def _expected_step_status(self, step: CollaborationStep) -> CollaborationStepStatus:
        if step.job_id is None:
            if step.requires_confirmation and step.confirmed_at is None:
                return CollaborationStepStatus.WAITING_FOR_CONFIRMATION
            return CollaborationStepStatus.PENDING
        job = self.stores.jobs.load(step.job_id)
        return self._step_status_for_job_status(job.status)

    @staticmethod
    def _step_status_for_job_status(status: JobStatus) -> CollaborationStepStatus:
        return {
            JobStatus.CREATED: CollaborationStepStatus.CREATED,
            JobStatus.RUNNING: CollaborationStepStatus.RUNNING,
            JobStatus.STOPPING: CollaborationStepStatus.RUNNING,
            JobStatus.STOPPED: CollaborationStepStatus.STOPPED,
            JobStatus.SUCCEEDED: CollaborationStepStatus.SUCCEEDED,
            JobStatus.FAILED: CollaborationStepStatus.FAILED,
            JobStatus.RESTORABLE: CollaborationStepStatus.RESTORABLE,
        }[status]

    @staticmethod
    def _derive_collaboration_status(
        steps: list[CollaborationStep],
    ) -> CollaborationStatus:
        if not steps:
            return CollaborationStatus.CREATED
        statuses = {step.status for step in steps}
        if CollaborationStepStatus.RESTORABLE in statuses:
            return CollaborationStatus.RESTORABLE
        active_statuses = {
            CollaborationStepStatus.CREATED,
            CollaborationStepStatus.RUNNING,
            CollaborationStepStatus.PENDING,
        }
        if statuses & active_statuses:
            return CollaborationStatus.RUNNING
        if CollaborationStepStatus.WAITING_FOR_CONFIRMATION in statuses:
            return CollaborationStatus.WAITING_FOR_CONFIRMATION
        failed_statuses = {CollaborationStepStatus.FAILED, CollaborationStepStatus.STOPPED}
        if statuses & failed_statuses:
            return CollaborationStatus.FAILED
        if statuses == {CollaborationStepStatus.SUCCEEDED}:
            return CollaborationStatus.SUCCEEDED
        return CollaborationStatus.CREATED

    def _normalize_artifact_inputs(
        self,
        artifact_inputs: list[ArtifactInput | dict[str, Any]] | None,
    ) -> list[ArtifactInput]:
        normalized: list[ArtifactInput] = []
        for artifact_input in artifact_inputs or []:
            if isinstance(artifact_input, ArtifactInput):
                normalized.append(artifact_input)
            else:
                normalized.append(ArtifactInput.from_dict(artifact_input))
        return normalized

    def _normalize_dependencies(
        self,
        dependencies: list[str] | None,
        artifact_inputs: list[ArtifactInput],
    ) -> list[str]:
        normalized: list[str] = []
        for dependency_id in [
            *(dependencies or []),
            *(artifact_input.source_job_id for artifact_input in artifact_inputs),
        ]:
            if dependency_id not in normalized:
                self.stores.jobs.load(dependency_id)
                normalized.append(dependency_id)
        return normalized

    async def _prepare_dependencies(self, job_id: str) -> None:
        job = self.stores.jobs.load(job_id)
        if job.dependencies:
            await self._record_and_publish(
                JobEvent.status(
                    job_id,
                    "job checking dependencies",
                    dependencies=list(job.dependencies),
                )
            )
        for dependency_id in job.dependencies or []:
            dependency = self.stores.jobs.load(dependency_id)
            if dependency.status != JobStatus.SUCCEEDED:
                raise DependencyError(
                    f"dependency {dependency_id} is {dependency.status.value}; job will not run"
                )

        for artifact_input in job.artifact_inputs or []:
            try:
                target_path = self.stores.copy_artifact_to_workspace(job_id, artifact_input)
            except FileNotFoundError:
                if artifact_input.optional:
                    await self._record_and_publish(
                        JobEvent.status(
                            job_id,
                            "artifact input skipped",
                            source_job_id=artifact_input.source_job_id,
                            source_path=artifact_input.source_path,
                        )
                    )
                    continue
                raise DependencyError(
                    "artifact input missing: "
                    f"{artifact_input.source_job_id}/{artifact_input.source_path}"
                ) from None
            await self._record_and_publish(
                JobEvent.status(
                    job_id,
                    "artifact input copied",
                    source_job_id=artifact_input.source_job_id,
                    source_path=artifact_input.source_path,
                    target_path=target_path,
                )
            )
