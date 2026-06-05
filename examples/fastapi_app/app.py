from __future__ import annotations

import json
import os
import shlex
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from keel_runtime import AgentSpec, JobManager, PiRpcRuntime
from keel_runtime.errors import JobNotFoundError


def _runtime_from_env() -> PiRpcRuntime:
    command = os.getenv("KEEL_PI_COMMAND")
    return PiRpcRuntime(command=shlex.split(command) if command else None)


app = FastAPI(title="Keel Runtime Example")
manager = JobManager(root=os.getenv("KEEL_DATA_DIR", ".keel"), runtime=_runtime_from_env())


class CreateJobRequest(BaseModel):
    spec: dict[str, Any] = Field(default_factory=dict)
    input: Any
    workspace: str | None = None


@app.post("/jobs")
def create_job(request: CreateJobRequest) -> dict[str, str]:
    spec_data = {"name": "default", **request.spec}
    job_id = manager.create_job(
        AgentSpec.from_dict(spec_data),
        input=request.input,
        workspace=request.workspace,
    )
    return {"job_id": job_id}


@app.get("/jobs")
def list_jobs() -> list[dict[str, Any]]:
    return [job.to_dict() for job in manager.list_jobs()]


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    try:
        return manager.get_job(job_id).to_dict()
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


@app.post("/jobs/{job_id}/stop")
async def stop_job(job_id: str) -> dict[str, str]:
    try:
        status = await manager.stop(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": status.value}


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
