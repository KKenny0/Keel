"""Agent job models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from keel_runtime.events import utc_now
from keel_runtime.specs import AgentSpec


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
        )

    def with_status(
        self,
        status: JobStatus,
        *,
        result: str | None = None,
        error: str | None = None,
    ) -> AgentJob:
        self.status = status
        self.updated_at = utc_now()
        if result is not None:
            self.result = result
        if error is not None:
            self.error = error
        return self
