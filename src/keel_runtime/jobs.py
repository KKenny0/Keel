"""Agent job models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from keel_runtime.events import utc_now
from keel_runtime.models import ModelUsage
from keel_runtime.specs import AgentSpec

_NOT_SET = object()


class JobStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RESTORABLE = "restorable"

    @property
    def is_terminal(self) -> bool:
        return self in {self.STOPPED, self.SUCCEEDED, self.FAILED}


@dataclass(slots=True)
class ArtifactInput:
    source_job_id: str
    source_path: str
    target_path: str | None = None
    optional: bool = False

    def __post_init__(self) -> None:
        if not self.source_job_id.strip():
            raise ValueError("ArtifactInput.source_job_id cannot be empty")
        if not self.source_path.strip():
            raise ValueError("ArtifactInput.source_path cannot be empty")
        if self.target_path is not None and not self.target_path.strip():
            raise ValueError("ArtifactInput.target_path cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_job_id": self.source_job_id,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "optional": self.optional,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArtifactInput:
        return cls(
            source_job_id=str(data["source_job_id"]),
            source_path=str(data["source_path"]),
            target_path=str(data["target_path"]) if data.get("target_path") is not None else None,
            optional=bool(data.get("optional", False)),
        )


@dataclass(slots=True)
class AgentJob:
    id: str
    spec: AgentSpec
    input: Any
    status: JobStatus
    session_path: str
    workspace_path: str
    artifact_path: str
    created_at: datetime
    updated_at: datetime
    result: str | None = None
    error: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    dependencies: list[str] | None = None
    artifact_inputs: list[ArtifactInput] | None = None
    model_usage: ModelUsage | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "spec": self.spec.to_dict(),
            "input": self.input,
            "status": self.status.value,
            "session_path": self.session_path,
            "workspace_path": self.workspace_path,
            "artifact_path": self.artifact_path,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "result": self.result,
            "error": self.error,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "dependencies": list(self.dependencies or []),
            "artifact_inputs": [
                artifact_input.to_dict() for artifact_input in (self.artifact_inputs or [])
            ],
            "model_usage": self.model_usage.to_dict() if self.model_usage else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentJob:
        return cls(
            id=data["id"],
            spec=AgentSpec.from_dict(data["spec"]),
            input=data.get("input"),
            status=JobStatus(data["status"]),
            session_path=data["session_path"],
            workspace_path=data["workspace_path"],
            artifact_path=data["artifact_path"],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            result=data.get("result"),
            error=data.get("error"),
            exit_code=data.get("exit_code"),
            timed_out=bool(data.get("timed_out", False)),
            dependencies=[str(job_id) for job_id in data.get("dependencies") or []],
            artifact_inputs=[
                ArtifactInput.from_dict(item) for item in data.get("artifact_inputs") or []
            ],
            model_usage=(
                ModelUsage.from_dict(data["model_usage"])
                if data.get("model_usage") is not None
                else None
            ),
        )

    def with_status(
        self,
        status: JobStatus,
        *,
        result: str | None | object = _NOT_SET,
        error: str | None | object = _NOT_SET,
        exit_code: int | None | object = _NOT_SET,
        timed_out: bool | object = _NOT_SET,
    ) -> AgentJob:
        self.status = status
        self.updated_at = utc_now()
        if result is not _NOT_SET:
            self.result = result
        if error is not _NOT_SET:
            self.error = error
        if exit_code is not _NOT_SET:
            self.exit_code = exit_code
        if timed_out is not _NOT_SET:
            self.timed_out = bool(timed_out)
        return self
