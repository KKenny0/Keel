"""Events emitted while an agent job runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    STATUS = "status"
    OUTPUT = "output"
    LOG = "log"
    ARTIFACT = "artifact"
    RESULT = "result"
    ERROR = "error"


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True)
class JobEvent:
    job_id: str
    type: EventType
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "type": self.type.value,
            "message": self.message,
            "data": self.data,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobEvent:
        return cls(
            job_id=data["job_id"],
            type=EventType(data["type"]),
            message=data["message"],
            data=dict(data.get("data") or {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    @classmethod
    def status(cls, job_id: str, message: str, **data: Any) -> JobEvent:
        return cls(job_id=job_id, type=EventType.STATUS, message=message, data=data)

    @classmethod
    def output(cls, job_id: str, message: str, **data: Any) -> JobEvent:
        return cls(job_id=job_id, type=EventType.OUTPUT, message=message, data=data)

    @classmethod
    def log(cls, job_id: str, message: str, **data: Any) -> JobEvent:
        return cls(job_id=job_id, type=EventType.LOG, message=message, data=data)

    @classmethod
    def error(cls, job_id: str, message: str, **data: Any) -> JobEvent:
        return cls(job_id=job_id, type=EventType.ERROR, message=message, data=data)
