"""Agent specification models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from keel_runtime.security import collect_secret_values, sanitize_env, sanitize_secret_env


@dataclass(slots=True)
class ResourceLimits:
    cpu: str | None = None
    memory: str | None = None
    ephemeral_storage: str | None = None

    def __post_init__(self) -> None:
        for name, value in self.to_dict().items():
            if value is not None and not str(value).strip():
                raise ValueError(f"ResourceLimits.{name} cannot be empty")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "cpu": self.cpu,
            "memory": self.memory,
            "ephemeral_storage": self.ephemeral_storage,
        }

    def to_docker_args(self) -> list[str]:
        args: list[str] = []
        if self.cpu:
            args.extend(["--cpus", self.cpu])
        if self.memory:
            args.extend(["--memory", self.memory])
        return args

    def to_kubernetes_resources(self) -> dict[str, str]:
        resources: dict[str, str] = {}
        if self.cpu:
            resources["cpu"] = self.cpu
        if self.memory:
            resources["memory"] = self.memory
        if self.ephemeral_storage:
            resources["ephemeral-storage"] = self.ephemeral_storage
        return resources

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ResourceLimits:
        data = data or {}
        return cls(
            cpu=data.get("cpu"),
            memory=data.get("memory"),
            ephemeral_storage=data.get("ephemeral_storage") or data.get("ephemeral-storage"),
        )


@dataclass(slots=True)
class AgentSpec:
    name: str
    system_prompt: str = ""
    skills: list[str] = field(default_factory=list)
    tools: dict[str, Any] = field(default_factory=dict)
    model: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    secret_env: dict[str, str] = field(default_factory=dict)
    command: list[str] | None = None
    timeout_seconds: float | None = None
    resources: ResourceLimits = field(default_factory=ResourceLimits)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("AgentSpec.name cannot be empty")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("AgentSpec.timeout_seconds must be positive")
        if isinstance(self.resources, dict):
            self.resources = ResourceLimits.from_dict(self.resources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "system_prompt": self.system_prompt,
            "skills": list(self.skills),
            "tools": self.tools,
            "model": self.model,
            "env": sanitize_env(self.env),
            "secret_env": sanitize_secret_env(self.secret_env),
            "command": list(self.command) if self.command is not None else None,
            "timeout_seconds": self.timeout_seconds,
            "resources": self.resources.to_dict(),
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
            secret_env={
                str(key): str(value) for key, value in dict(data.get("secret_env") or {}).items()
            },
            command=list(data["command"]) if data.get("command") is not None else None,
            timeout_seconds=data.get("timeout_seconds"),
            resources=ResourceLimits.from_dict(data.get("resources")),
        )

    def runtime_env(self) -> dict[str, str]:
        env = {str(key): str(value) for key, value in self.env.items()}
        env.update({str(key): str(value) for key, value in self.secret_env.items()})
        return env

    def secret_values(self) -> list[str]:
        return collect_secret_values(self.env, self.secret_env)
