"""Agent specification models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AgentSpec:
    name: str
    system_prompt: str = ""
    skills: list[str] = field(default_factory=list)
    tools: dict[str, Any] = field(default_factory=dict)
    model: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    command: list[str] | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("AgentSpec.name cannot be empty")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("AgentSpec.timeout_seconds must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "system_prompt": self.system_prompt,
            "skills": list(self.skills),
            "tools": self.tools,
            "model": self.model,
            "env": self.env,
            "command": list(self.command) if self.command is not None else None,
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentSpec:
        return cls(
            name=data["name"],
            system_prompt=data.get("system_prompt") or "",
            skills=list(data.get("skills") or []),
            tools=dict(data.get("tools") or {}),
            model=dict(data.get("model") or {}),
            env={str(key): str(value) for key, value in dict(data.get("env") or {}).items()},
            command=list(data["command"]) if data.get("command") is not None else None,
            timeout_seconds=data.get("timeout_seconds"),
        )
