"""Human approval gate primitives."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from keel_runtime.events import JobEvent, utc_now
from keel_runtime.tools import ToolSpec


class GateDecisionStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


@dataclass(slots=True)
class GateRequest:
    id: str
    action: str
    reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("GateRequest.id cannot be empty")
        if not self.action.strip():
            raise ValueError("GateRequest.action cannot be empty")
        if self.timeout_seconds is not None and self.timeout_seconds < 0:
            raise ValueError("GateRequest.timeout_seconds cannot be negative")

    @classmethod
    def create(
        cls,
        action: str,
        *,
        reason: str = "",
        payload: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> GateRequest:
        return cls(
            id=uuid.uuid4().hex,
            action=action,
            reason=reason,
            payload=dict(payload or {}),
            timeout_seconds=timeout_seconds,
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "reason": self.reason,
            "payload": dict(self.payload),
            "timeout_seconds": self.timeout_seconds,
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GateRequest:
        return cls(
            id=str(data["id"]),
            action=str(data["action"]),
            reason=str(data.get("reason") or ""),
            payload=dict(data.get("payload") or {}),
            timeout_seconds=(
                float(data["timeout_seconds"])
                if data.get("timeout_seconds") is not None
                else None
            ),
            metadata=dict(data.get("metadata") or {}),
            created_at=(
                datetime.fromisoformat(data["created_at"])
                if data.get("created_at")
                else utc_now()
            ),
        )


@dataclass(slots=True)
class GateDecision:
    request_id: str
    status: GateDecisionStatus
    feedback: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    decided_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ValueError("GateDecision.request_id cannot be empty")
        if not isinstance(self.status, GateDecisionStatus):
            self.status = GateDecisionStatus(self.status)

    @property
    def approved(self) -> bool:
        return self.status == GateDecisionStatus.APPROVED

    @property
    def rejected(self) -> bool:
        return self.status == GateDecisionStatus.REJECTED

    @property
    def timed_out(self) -> bool:
        return self.status == GateDecisionStatus.TIMEOUT

    @classmethod
    def approve(
        cls,
        request_id: str,
        *,
        feedback: str = "",
        payload: dict[str, Any] | None = None,
    ) -> GateDecision:
        return cls(
            request_id=request_id,
            status=GateDecisionStatus.APPROVED,
            feedback=feedback,
            payload=dict(payload or {}),
        )

    @classmethod
    def reject(
        cls,
        request_id: str,
        *,
        feedback: str = "",
        payload: dict[str, Any] | None = None,
    ) -> GateDecision:
        return cls(
            request_id=request_id,
            status=GateDecisionStatus.REJECTED,
            feedback=feedback,
            payload=dict(payload or {}),
        )

    @classmethod
    def timeout(
        cls,
        request_id: str,
        *,
        feedback: str = "human gate timed out",
    ) -> GateDecision:
        return cls(
            request_id=request_id,
            status=GateDecisionStatus.TIMEOUT,
            feedback=feedback,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status.value,
            "approved": self.approved,
            "feedback": self.feedback,
            "payload": dict(self.payload),
            "decided_at": self.decided_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GateDecision:
        return cls(
            request_id=str(data["request_id"]),
            status=GateDecisionStatus(data["status"]),
            feedback=str(data.get("feedback") or ""),
            payload=dict(data.get("payload") or {}),
            decided_at=(
                datetime.fromisoformat(data["decided_at"])
                if data.get("decided_at")
                else utc_now()
            ),
        )


class HumanGate:
    def __init__(
        self,
        decisions: Iterable[GateDecision | GateDecisionStatus | str | bool] | None = None,
        *,
        default_timeout_seconds: float = 30,
    ) -> None:
        if default_timeout_seconds < 0:
            raise ValueError("default_timeout_seconds cannot be negative")
        self.default_timeout_seconds = default_timeout_seconds
        self.requests: list[GateRequest] = []
        self.decisions: list[GateDecision] = []
        self._queued_decisions = list(decisions or [])
        self._pending: dict[str, asyncio.Future[GateDecision]] = {}
        self._events: list[JobEvent] = []

    async def request(self, request: GateRequest, *, job_id: str = "agent-loop") -> GateDecision:
        self.requests.append(request)
        self._events.append(
            JobEvent.status(
                job_id,
                "gate requested",
                request=request.to_dict(),
            )
        )
        decision = await self._resolve_decision(request)
        self.decisions.append(decision)
        self._events.append(self._decision_event(job_id, decision))
        return decision

    def approve(
        self,
        request_id: str,
        *,
        feedback: str = "",
        payload: dict[str, Any] | None = None,
    ) -> GateDecision:
        decision = GateDecision.approve(request_id, feedback=feedback, payload=payload)
        self._resolve_pending(decision)
        return decision

    def reject(
        self,
        request_id: str,
        *,
        feedback: str = "",
        payload: dict[str, Any] | None = None,
    ) -> GateDecision:
        decision = GateDecision.reject(request_id, feedback=feedback, payload=payload)
        self._resolve_pending(decision)
        return decision

    def queue_decision(self, decision: GateDecision | GateDecisionStatus | str | bool) -> None:
        self._queued_decisions.append(decision)

    def as_tool(
        self,
        *,
        name: str = "human_gate",
        job_id: str = "agent-loop",
    ) -> ToolSpec:
        async def request_human_gate(
            action: str,
            reason: str = "",
            payload: dict[str, Any] | None = None,
            timeout_seconds: float | None = None,
        ) -> dict[str, Any]:
            gate_request = GateRequest.create(
                action,
                reason=reason,
                payload=payload,
                timeout_seconds=timeout_seconds,
            )
            decision = await self.request(gate_request, job_id=job_id)
            return decision.to_dict()

        return ToolSpec.from_callable(
            request_human_gate,
            name=name,
            description="Pause for human approval before continuing.",
        )

    def drain_events(self) -> list[JobEvent]:
        events = list(self._events)
        self._events.clear()
        return events

    async def _resolve_decision(self, request: GateRequest) -> GateDecision:
        if self._queued_decisions:
            return _normalize_decision(self._queued_decisions.pop(0), request.id)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[GateDecision] = loop.create_future()
        self._pending[request.id] = future
        timeout = (
            request.timeout_seconds
            if request.timeout_seconds is not None
            else self.default_timeout_seconds
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            return GateDecision.timeout(request.id)
        finally:
            self._pending.pop(request.id, None)

    def _resolve_pending(self, decision: GateDecision) -> None:
        future = self._pending.get(decision.request_id)
        if future is not None and not future.done():
            future.set_result(decision)

    @staticmethod
    def _decision_event(job_id: str, decision: GateDecision) -> JobEvent:
        message = {
            GateDecisionStatus.APPROVED: "gate approved",
            GateDecisionStatus.REJECTED: "gate rejected",
            GateDecisionStatus.TIMEOUT: "gate timeout",
        }[decision.status]
        return JobEvent.status(job_id, message, decision=decision.to_dict())


def _normalize_decision(
    decision: GateDecision | GateDecisionStatus | str | bool,
    request_id: str,
) -> GateDecision:
    if isinstance(decision, GateDecision):
        if decision.request_id == request_id:
            return decision
        return GateDecision(
            request_id=request_id,
            status=decision.status,
            feedback=decision.feedback,
            payload=dict(decision.payload),
            decided_at=decision.decided_at,
        )
    if isinstance(decision, bool):
        return (
            GateDecision.approve(request_id)
            if decision
            else GateDecision.reject(request_id)
        )
    status = GateDecisionStatus(decision)
    if status == GateDecisionStatus.APPROVED:
        return GateDecision.approve(request_id)
    if status == GateDecisionStatus.REJECTED:
        return GateDecision.reject(request_id)
    return GateDecision.timeout(request_id)
