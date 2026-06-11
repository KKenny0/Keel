"""Memory provider primitives with a local keyword-only implementation."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from keel_runtime.events import utc_now
from keel_runtime.tools import ToolSpec


@dataclass(slots=True)
class Decision:
    id: str
    scope: str
    title: str
    outcome: str
    rationale: str = ""
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Decision.id cannot be empty")
        if not self.scope.strip():
            raise ValueError("Decision.scope cannot be empty")
        if not self.title.strip():
            raise ValueError("Decision.title cannot be empty")
        if not self.outcome.strip():
            raise ValueError("Decision.outcome cannot be empty")

    @classmethod
    def create(
        cls,
        title: str,
        outcome: str,
        *,
        scope: str = "default",
        rationale: str = "",
        tags: Sequence[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Decision:
        return cls(
            id=uuid.uuid4().hex,
            scope=scope,
            title=title,
            outcome=outcome,
            rationale=rationale,
            tags=[str(tag) for tag in tags or []],
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "title": self.title,
            "outcome": self.outcome,
            "rationale": self.rationale,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Decision:
        return cls(
            id=str(data["id"]),
            scope=str(data["scope"]),
            title=str(data["title"]),
            outcome=str(data["outcome"]),
            rationale=str(data.get("rationale") or ""),
            tags=[str(tag) for tag in data.get("tags") or []],
            metadata=dict(data.get("metadata") or {}),
            created_at=(
                datetime.fromisoformat(data["created_at"])
                if data.get("created_at")
                else utc_now()
            ),
        )


class MemoryProvider(Protocol):
    async def record_decision(self, decision: Decision) -> Decision:
        """Persist a structured decision."""

    async def recall(
        self,
        query: str,
        *,
        scope: str = "default",
        limit: int = 5,
    ) -> list[Decision]:
        """Recall decisions with provider-defined matching."""


class LocalMemoryProvider:
    """Local JSONL memory store using keyword matching, not semantic search."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._decisions: list[Decision] = []
        if self.path is not None and self.path.exists():
            self._decisions = self._load()

    async def record_decision(self, decision: Decision) -> Decision:
        self._decisions.append(decision)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(decision.to_dict(), ensure_ascii=False) + "\n")
        return decision

    async def recall(
        self,
        query: str,
        *,
        scope: str = "default",
        limit: int = 5,
    ) -> list[Decision]:
        if limit <= 0:
            return []
        scoped = [decision for decision in self._decisions if decision.scope == scope]
        terms = _query_terms(query)
        if not terms:
            return list(reversed(scoped))[:limit]

        ranked: list[tuple[int, float, Decision]] = []
        for decision in scoped:
            score = _keyword_score(terms, decision)
            if score > 0:
                ranked.append((score, decision.created_at.timestamp(), decision))
        ranked.sort(key=lambda item: (-item[0], -item[1]))
        return [decision for _, _, decision in ranked[:limit]]

    def list_decisions(self, *, scope: str | None = None) -> list[Decision]:
        if scope is None:
            return list(self._decisions)
        return [decision for decision in self._decisions if decision.scope == scope]

    def _load(self) -> list[Decision]:
        decisions: list[Decision] = []
        assert self.path is not None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            decisions.append(Decision.from_dict(json.loads(line)))
        return decisions


def memory_tools(
    provider: MemoryProvider,
    *,
    default_scope: str = "default",
) -> list[ToolSpec]:
    async def memory_recall(
        query: str,
        scope: str = default_scope,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        records = await provider.recall(query, scope=scope, limit=limit)
        return [record.to_dict() for record in records]

    async def memory_record(
        title: str,
        outcome: str,
        rationale: str = "",
        scope: str = default_scope,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        decision = Decision.create(
            title,
            outcome,
            scope=scope,
            rationale=rationale,
            tags=tags,
            metadata=metadata,
        )
        recorded = await provider.record_decision(decision)
        return recorded.to_dict()

    return [
        ToolSpec.from_callable(
            memory_recall,
            name="memory_recall",
            description="Recall recorded decisions by keyword within a scope.",
        ),
        ToolSpec.from_callable(
            memory_record,
            name="memory_record",
            description="Record a structured decision in memory.",
        ),
    ]


def _query_terms(query: str) -> list[str]:
    return [term for term in query.lower().split() if term]


def _keyword_score(terms: Sequence[str], decision: Decision) -> int:
    searchable = " ".join(
        [
            decision.title,
            decision.outcome,
            decision.rationale,
            " ".join(decision.tags),
            json.dumps(decision.metadata, ensure_ascii=False, sort_keys=True),
        ]
    ).lower()
    return sum(1 for term in terms if term in searchable)
