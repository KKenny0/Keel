"""Lightweight multi-agent collaboration models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from keel_runtime.events import utc_now
from keel_runtime.jobs import ArtifactInput
from keel_runtime.specs import AgentSpec


class CollaborationStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RESTORABLE = "restorable"


class CollaborationStepStatus(StrEnum):
    PENDING = "pending"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"
    RESTORABLE = "restorable"


@dataclass(slots=True)
class CollaborationStep:
    id: str
    agent_name: str
    spec: AgentSpec
    input: Any
    status: CollaborationStepStatus
    created_at: datetime
    updated_at: datetime
    dependencies: list[str] = field(default_factory=list)
    artifact_inputs: list[ArtifactInput] = field(default_factory=list)
    requires_confirmation: bool = False
    confirmed_at: datetime | None = None
    confirmation_note: str | None = None
    job_ids: list[str] = field(default_factory=list)
    max_attempts: int = 2
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("CollaborationStep.id cannot be empty")
        if not self.agent_name.strip():
            raise ValueError("CollaborationStep.agent_name cannot be empty")
        if self.max_attempts < 1:
            raise ValueError("CollaborationStep.max_attempts must be at least 1")

    @property
    def job_id(self) -> str | None:
        if not self.job_ids:
            return None
        return self.job_ids[-1]

    @property
    def attempt(self) -> int:
        return len(self.job_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "spec": self.spec.to_dict(),
            "input": self.input,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "dependencies": list(self.dependencies),
            "artifact_inputs": [
                artifact_input.to_dict() for artifact_input in self.artifact_inputs
            ],
            "requires_confirmation": self.requires_confirmation,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "confirmation_note": self.confirmation_note,
            "job_ids": list(self.job_ids),
            "job_id": self.job_id,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "context": dict(self.context),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollaborationStep:
        job_ids = [str(job_id) for job_id in data.get("job_ids") or []]
        if not job_ids and data.get("job_id"):
            job_ids.append(str(data["job_id"]))
        return cls(
            id=str(data["id"]),
            agent_name=str(data["agent_name"]),
            spec=AgentSpec.from_dict(data["spec"]),
            input=data.get("input"),
            status=CollaborationStepStatus(data["status"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            dependencies=[str(job_id) for job_id in data.get("dependencies") or []],
            artifact_inputs=[
                ArtifactInput.from_dict(item) for item in data.get("artifact_inputs") or []
            ],
            requires_confirmation=bool(data.get("requires_confirmation", False)),
            confirmed_at=(
                datetime.fromisoformat(data["confirmed_at"])
                if data.get("confirmed_at")
                else None
            ),
            confirmation_note=data.get("confirmation_note"),
            job_ids=job_ids,
            max_attempts=int(data.get("max_attempts", 2)),
            context=dict(data.get("context") or {}),
        )

    def with_status(self, status: CollaborationStepStatus) -> CollaborationStep:
        self.status = status
        self.updated_at = utc_now()
        return self


@dataclass(slots=True)
class Collaboration:
    id: str
    goal: str
    status: CollaborationStatus
    project_workspace_path: str
    context: dict[str, Any]
    steps: list[CollaborationStep]
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Collaboration.id cannot be empty")
        if not self.goal.strip():
            raise ValueError("Collaboration.goal cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status.value,
            "project_workspace_path": self.project_workspace_path,
            "context": dict(self.context),
            "steps": [step.to_dict() for step in self.steps],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Collaboration:
        return cls(
            id=str(data["id"]),
            goal=str(data["goal"]),
            status=CollaborationStatus(data["status"]),
            project_workspace_path=str(data["project_workspace_path"]),
            context=dict(data.get("context") or {}),
            steps=[CollaborationStep.from_dict(item) for item in data.get("steps") or []],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    def with_status(self, status: CollaborationStatus) -> Collaboration:
        self.status = status
        self.updated_at = utc_now()
        return self
