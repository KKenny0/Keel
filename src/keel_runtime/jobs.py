"""Agent job models."""

from __future__ import annotations

from dataclasses import dataclass, field
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


class JobAttemptKind(StrEnum):
    INITIAL = "initial"
    RETRY = "retry"
    RESUME = "resume"


class JobAttemptStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"


class AgentLoopCheckpointStatus(StrEnum):
    RUNNING = "running"
    AWAITING_TOOL = "awaiting_tool"
    AWAITING_GATE = "awaiting_gate"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class JobAttempt:
    id: str
    job_id: str
    number: int
    kind: JobAttemptKind
    status: JobAttemptStatus
    started_at: datetime
    retry_of: str | None = None
    resume_of: str | None = None
    idempotency_key: str | None = None
    ended_at: datetime | None = None
    error: str | None = None
    retryable: bool | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("JobAttempt.id cannot be empty")
        if not self.job_id.strip():
            raise ValueError("JobAttempt.job_id cannot be empty")
        if self.number <= 0:
            raise ValueError("JobAttempt.number must be positive")
        if isinstance(self.kind, str):
            self.kind = JobAttemptKind(self.kind)
        if isinstance(self.status, str):
            self.status = JobAttemptStatus(self.status)
        if self.retry_of is not None and not self.retry_of.strip():
            raise ValueError("JobAttempt.retry_of cannot be empty")
        if self.resume_of is not None and not self.resume_of.strip():
            raise ValueError("JobAttempt.resume_of cannot be empty")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ValueError("JobAttempt.idempotency_key cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "number": self.number,
            "kind": self.kind.value,
            "retry_of": self.retry_of,
            "resume_of": self.resume_of,
            "idempotency_key": self.idempotency_key,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "error": self.error,
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobAttempt:
        return cls(
            id=str(data["id"]),
            job_id=str(data["job_id"]),
            number=int(data["number"]),
            kind=JobAttemptKind(data["kind"]),
            retry_of=str(data["retry_of"]) if data.get("retry_of") is not None else None,
            resume_of=str(data["resume_of"]) if data.get("resume_of") is not None else None,
            idempotency_key=(
                str(data["idempotency_key"])
                if data.get("idempotency_key") is not None
                else None
            ),
            status=JobAttemptStatus(data["status"]),
            started_at=datetime.fromisoformat(data["started_at"]),
            ended_at=(
                datetime.fromisoformat(data["ended_at"])
                if data.get("ended_at") is not None
                else None
            ),
            error=str(data["error"]) if data.get("error") is not None else None,
            retryable=(
                bool(data["retryable"]) if data.get("retryable") is not None else None
            ),
        )


@dataclass(slots=True)
class AgentLoopCheckpoint:
    version: int
    job_id: str
    attempt_id: str
    agent_name: str
    iteration: int
    status: AgentLoopCheckpointStatus
    created_at: datetime
    updated_at: datetime
    history_messages: list[dict[str, Any]] = field(default_factory=list)
    active_messages: list[dict[str, Any]] = field(default_factory=list)
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    completed_tool_results: list[dict[str, Any]] = field(default_factory=list)
    context_results: list[dict[str, Any]] = field(default_factory=list)
    composed_prompts: list[dict[str, Any]] = field(default_factory=list)
    gate_decisions: list[dict[str, Any]] = field(default_factory=list)
    raw_output: str | None = None
    parsed_output: Any = None
    parse_error: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.version <= 0:
            raise ValueError("AgentLoopCheckpoint.version must be positive")
        if not self.job_id.strip():
            raise ValueError("AgentLoopCheckpoint.job_id cannot be empty")
        if not self.attempt_id.strip():
            raise ValueError("AgentLoopCheckpoint.attempt_id cannot be empty")
        if not self.agent_name.strip():
            raise ValueError("AgentLoopCheckpoint.agent_name cannot be empty")
        if self.iteration < 0:
            raise ValueError("AgentLoopCheckpoint.iteration cannot be negative")
        if isinstance(self.status, str):
            self.status = AgentLoopCheckpointStatus(self.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "agent_name": self.agent_name,
            "iteration": self.iteration,
            "status": self.status.value,
            "history_messages": [dict(item) for item in self.history_messages],
            "active_messages": [dict(item) for item in self.active_messages],
            "pending_tool_calls": [dict(item) for item in self.pending_tool_calls],
            "completed_tool_results": [
                dict(item) for item in self.completed_tool_results
            ],
            "context_results": [dict(item) for item in self.context_results],
            "composed_prompts": [dict(item) for item in self.composed_prompts],
            "gate_decisions": [dict(item) for item in self.gate_decisions],
            "raw_output": self.raw_output,
            "parsed_output": self.parsed_output,
            "parse_error": dict(self.parse_error) if self.parse_error is not None else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentLoopCheckpoint:
        return cls(
            version=int(data["version"]),
            job_id=str(data["job_id"]),
            attempt_id=str(data["attempt_id"]),
            agent_name=str(data["agent_name"]),
            iteration=int(data["iteration"]),
            status=AgentLoopCheckpointStatus(data["status"]),
            history_messages=[dict(item) for item in data.get("history_messages") or []],
            active_messages=[dict(item) for item in data.get("active_messages") or []],
            pending_tool_calls=[
                dict(item) for item in data.get("pending_tool_calls") or []
            ],
            completed_tool_results=[
                dict(item) for item in data.get("completed_tool_results") or []
            ],
            context_results=[dict(item) for item in data.get("context_results") or []],
            composed_prompts=[dict(item) for item in data.get("composed_prompts") or []],
            gate_decisions=[dict(item) for item in data.get("gate_decisions") or []],
            raw_output=str(data["raw_output"]) if data.get("raw_output") is not None else None,
            parsed_output=data.get("parsed_output"),
            parse_error=dict(data["parse_error"]) if data.get("parse_error") else None,
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )


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
