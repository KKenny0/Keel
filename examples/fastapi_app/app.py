from __future__ import annotations

import json
import os
import shlex
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from keel_runtime import (
    AgentSpec,
    CleanupPolicy,
    DockerRuntime,
    JobManager,
    KubernetesPodRuntime,
    PiRpcRuntime,
    S3ObjectStorage,
)
from keel_runtime.errors import CollaborationNotFoundError, InvalidJobStateError, JobNotFoundError


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _runtime_from_env():
    mode = os.getenv("KEEL_RUNTIME", "local").strip().lower()
    command = os.getenv("KEEL_PI_COMMAND")
    parsed_command = shlex.split(command) if command else None
    if mode == "docker":
        image = os.environ["KEEL_DOCKER_IMAGE"]
        docker_command = os.getenv("KEEL_DOCKER_COMMAND")
        return DockerRuntime(
            image=image,
            command=parsed_command,
            docker_command=shlex.split(docker_command) if docker_command else None,
            network=os.getenv("KEEL_DOCKER_NETWORK"),
        )
    if mode == "kubernetes":
        image = os.environ["KEEL_K8S_IMAGE"]
        pvc_name = os.environ["KEEL_K8S_PVC"]
        kubectl_command = os.getenv("KEEL_KUBECTL_COMMAND")
        return KubernetesPodRuntime(
            image=image,
            pvc_name=pvc_name,
            command=parsed_command,
            kubectl_command=shlex.split(kubectl_command) if kubectl_command else None,
            namespace=os.getenv("KEEL_K8S_NAMESPACE"),
            secret_name=os.getenv("KEEL_K8S_SECRET_NAME", "keel-agent-secrets"),
        )
    return PiRpcRuntime(command=parsed_command)


def _object_storage_from_env():
    if not os.getenv("KEEL_S3_BUCKET"):
        return None
    return S3ObjectStorage.from_env()


def _cleanup_policy_from_env() -> CleanupPolicy:
    return CleanupPolicy(
        remove_workspace_on_success=_env_bool("KEEL_CLEAN_WORKSPACE_ON_SUCCESS"),
        remove_workspace_on_failure=_env_bool("KEEL_CLEAN_WORKSPACE_ON_FAILURE"),
        remove_artifacts_on_success=_env_bool("KEEL_CLEAN_ARTIFACTS_ON_SUCCESS"),
        remove_artifacts_on_failure=_env_bool("KEEL_CLEAN_ARTIFACTS_ON_FAILURE"),
    )


app = FastAPI(title="Keel Runtime Example")
manager = JobManager(
    root=os.getenv("KEEL_DATA_DIR", ".keel"),
    runtime=_runtime_from_env(),
    object_storage=_object_storage_from_env(),
    cleanup_policy=_cleanup_policy_from_env(),
)


class CreateJobRequest(BaseModel):
    spec: dict[str, Any] = Field(default_factory=dict)
    input: Any
    workspace: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    artifact_inputs: list[dict[str, Any]] = Field(default_factory=list)


class CreateCollaborationRequest(BaseModel):
    goal: str
    workspace: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class CreateCollaborationStepRequest(BaseModel):
    spec: dict[str, Any] = Field(default_factory=dict)
    input: Any
    dependencies: list[str] = Field(default_factory=list)
    artifact_inputs: list[dict[str, Any]] = Field(default_factory=list)
    requires_confirmation: bool = False
    max_attempts: int = 2
    context: dict[str, Any] = Field(default_factory=dict)


class ConfirmCollaborationStepRequest(BaseModel):
    note: str | None = None


@app.post("/jobs")
def create_job(request: CreateJobRequest) -> dict[str, str]:
    spec_data = {"name": "default", **request.spec}
    job_id = manager.create_job(
        AgentSpec.from_dict(spec_data),
        input=request.input,
        workspace=request.workspace,
        dependencies=request.dependencies,
        artifact_inputs=request.artifact_inputs,
    )
    return {"job_id": job_id}


@app.post("/collaborations")
def create_collaboration(request: CreateCollaborationRequest) -> dict[str, str]:
    collaboration_id = manager.create_collaboration(
        goal=request.goal,
        workspace=request.workspace,
        context=request.context,
    )
    return {"collaboration_id": collaboration_id}


@app.get("/collaborations")
def list_collaborations() -> list[dict[str, Any]]:
    return [collaboration.to_dict() for collaboration in manager.list_collaborations()]


@app.get("/collaborations/{collaboration_id}")
def describe_collaboration(collaboration_id: str) -> dict[str, Any]:
    try:
        return manager.describe_collaboration(collaboration_id)
    except CollaborationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/collaborations/{collaboration_id}/steps")
def add_collaboration_step(
    collaboration_id: str,
    request: CreateCollaborationStepRequest,
) -> dict[str, str | None]:
    try:
        spec_data = {"name": "default", **request.spec}
        step_id = manager.add_collaboration_step(
            collaboration_id,
            AgentSpec.from_dict(spec_data),
            request.input,
            dependencies=request.dependencies,
            artifact_inputs=request.artifact_inputs,
            requires_confirmation=request.requires_confirmation,
            max_attempts=request.max_attempts,
            context=request.context,
        )
        step = manager.get_collaboration_step(collaboration_id, step_id)
        return {"step_id": step_id, "job_id": step.job_id}
    except (CollaborationNotFoundError, JobNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/collaborations/{collaboration_id}/steps/{step_id}/confirm")
def confirm_collaboration_step(
    collaboration_id: str,
    step_id: str,
    request: ConfirmCollaborationStepRequest,
) -> dict[str, str]:
    try:
        job_id = manager.confirm_collaboration_step(
            collaboration_id,
            step_id,
            note=request.note,
        )
        return {"job_id": job_id}
    except (CollaborationNotFoundError, JobNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidJobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/collaborations/{collaboration_id}/steps/{step_id}/retry")
def retry_collaboration_step(collaboration_id: str, step_id: str) -> dict[str, str]:
    try:
        return {"job_id": manager.retry_collaboration_step(collaboration_id, step_id)}
    except (CollaborationNotFoundError, JobNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidJobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/jobs")
def list_jobs() -> list[dict[str, Any]]:
    return [job.to_dict() for job in manager.list_jobs()]


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    try:
        return manager.get_job(job_id).to_dict()
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/jobs/{job_id}/summary")
def describe_job(job_id: str) -> dict[str, Any]:
    try:
        return manager.describe_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    async def events():
        try:
            async for event in manager.stream(job_id):
                yield f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
        except JobNotFoundError as exc:
            yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/collaborations/{collaboration_id}/steps/{step_id}/stream")
async def stream_collaboration_step(
    collaboration_id: str,
    step_id: str,
) -> StreamingResponse:
    async def events():
        try:
            async for event in manager.stream_collaboration_step(collaboration_id, step_id):
                yield f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
        except (CollaborationNotFoundError, JobNotFoundError) as exc:
            yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"
        except InvalidJobStateError as exc:
            yield f"event: error\ndata: {json.dumps({'detail': str(exc), 'status': 409})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/jobs/{job_id}/resume")
async def resume_job(job_id: str) -> StreamingResponse:
    async def events():
        try:
            async for event in manager.resume(job_id):
                yield f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
        except JobNotFoundError as exc:
            yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/collaborations/{collaboration_id}/steps/{step_id}/resume")
async def resume_collaboration_step(
    collaboration_id: str,
    step_id: str,
) -> StreamingResponse:
    async def events():
        try:
            async for event in manager.resume_collaboration_step(collaboration_id, step_id):
                yield f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
        except (CollaborationNotFoundError, JobNotFoundError) as exc:
            yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"
        except InvalidJobStateError as exc:
            yield f"event: error\ndata: {json.dumps({'detail': str(exc), 'status': 409})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/jobs/{job_id}/stop")
async def stop_job(job_id: str) -> dict[str, str]:
    try:
        status = await manager.stop(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": status.value}


@app.get("/jobs/{job_id}/record")
def get_job_record(job_id: str) -> dict[str, Any]:
    try:
        return manager.snapshot_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/jobs/{job_id}/export")
def export_job(job_id: str) -> dict[str, str]:
    try:
        return {"path": str(manager.export_job(job_id))}
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/jobs/{job_id}/cleanup")
def cleanup_job(job_id: str) -> dict[str, list[str]]:
    try:
        return {"removed": manager.cleanup_job(job_id)}
    except InvalidJobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/jobs/{job_id}/artifacts")
def list_artifacts(job_id: str) -> list[str]:
    try:
        return manager.list_artifacts(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/jobs/{job_id}/artifacts/{path:path}")
def download_artifact(job_id: str, path: str) -> Response:
    try:
        return Response(manager.download_artifact(job_id, path))
    except (FileNotFoundError, JobNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
