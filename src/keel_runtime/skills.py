"""Prompt composition primitives for agent skills."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(slots=True)
class AgentContext:
    task: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    history_count: int = 0
    active_count: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentContext:
        return cls(
            task=str(data.get("task") or ""),
            metadata=dict(data.get("metadata") or {}),
            history_count=int(data.get("history_count") or 0),
            active_count=int(data.get("active_count") or 0),
        )


@dataclass(slots=True)
class ComposedPrompt:
    content: str
    skill_names: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    examples: list[Any] = field(default_factory=list)
    schema_overrides: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class PromptComposer(Protocol):
    def compose(
        self,
        base_prompt: str,
        context: AgentContext,
    ) -> ComposedPrompt | Awaitable[ComposedPrompt]:
        """Compose the system prompt for an agent loop call."""


@dataclass(slots=True)
class _SkillDefinition:
    name: str
    description: str = ""
    enabled: bool = True
    priority: int = 0
    constraints: list[str] = field(default_factory=list)
    examples: list[Any] = field(default_factory=list)
    schema_overrides: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any], source_path: Path) -> _SkillDefinition:
        known = {
            "name",
            "description",
            "enabled",
            "priority",
            "constraints",
            "examples",
            "schema_overrides",
        }
        return cls(
            name=str(data.get("name") or source_path.stem),
            description=str(data.get("description") or ""),
            enabled=_bool_value(data.get("enabled", True)),
            priority=int(data.get("priority") or 0),
            constraints=_string_list(data.get("constraints")),
            examples=_value_list(data.get("examples")),
            schema_overrides=dict(data.get("schema_overrides") or {}),
            metadata={key: value for key, value in data.items() if key not in known},
            source_path=str(source_path),
        )


class FileSkillComposer:
    def __init__(
        self,
        paths: str | Path | Sequence[str | Path],
        *,
        include_globs: Sequence[str] = ("*.json", "*.yaml", "*.yml"),
    ) -> None:
        if isinstance(paths, str | Path):
            self.paths = [Path(paths)]
        else:
            self.paths = [Path(path) for path in paths]
        self.include_globs = tuple(include_globs)

    def compose(self, base_prompt: str, context: AgentContext) -> ComposedPrompt:
        skills = self._load_enabled_skills()
        return _compose_prompt(base_prompt, skills, context)

    def _load_enabled_skills(self) -> list[_SkillDefinition]:
        skills = [
            skill
            for path in self._skill_files()
            for skill in [_SkillDefinition.from_dict(_load_skill_file(path), path)]
            if skill.enabled
        ]
        return sorted(
            skills,
            key=lambda skill: (-skill.priority, skill.name.lower(), skill.source_path),
        )

    def _skill_files(self) -> list[Path]:
        files: list[Path] = []
        for path in self.paths:
            if path.is_dir():
                for pattern in self.include_globs:
                    files.extend(sorted(path.glob(pattern)))
            elif path.is_file():
                files.append(path)
            else:
                raise FileNotFoundError(f"Skill path does not exist: {path}")
        return sorted(set(files))


def _compose_prompt(
    base_prompt: str,
    skills: Sequence[_SkillDefinition],
    context: AgentContext,
) -> ComposedPrompt:
    constraints: list[str] = []
    examples: list[Any] = []
    schema_overrides: dict[str, Any] = {}
    skill_names: list[str] = []
    parts = [base_prompt.strip()] if base_prompt.strip() else []

    skill_lines: list[str] = []
    for skill in skills:
        skill_names.append(skill.name)
        if skill.description:
            skill_lines.append(f"- {skill.name}: {skill.description}")
        else:
            skill_lines.append(f"- {skill.name}")
        constraints.extend(skill.constraints)
        examples.extend(skill.examples)
        schema_overrides.update(skill.schema_overrides)

    if skill_lines:
        parts.append("Skills:\n" + "\n".join(skill_lines))
    if constraints:
        parts.append("Constraints:\n" + "\n".join(f"- {item}" for item in constraints))
    if examples:
        parts.append("Examples:\n" + "\n".join(_format_example(item) for item in examples))
    if schema_overrides:
        parts.append(
            "Schema Overrides:\n"
            + json.dumps(schema_overrides, ensure_ascii=False, sort_keys=True)
        )

    return ComposedPrompt(
        content="\n\n".join(parts),
        skill_names=skill_names,
        constraints=constraints,
        examples=examples,
        schema_overrides=schema_overrides,
        metadata={
            "history_count": context.history_count,
            "active_count": context.active_count,
            **context.metadata,
        },
    )


def _load_skill_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(text)
    elif suffix in {".yaml", ".yml"}:
        data = _parse_yaml_subset(text)
    else:
        raise ValueError(f"Unsupported skill file type: {path.suffix}")
    if not isinstance(data, dict):
        raise TypeError(f"Skill file must contain an object: {path}")
    return data


def _parse_yaml_subset(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_key: str | None = None
    current_indent = 0
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if line.startswith("- "):
            if current_key is None:
                raise ValueError("YAML list item has no parent key")
            if not isinstance(data[current_key], list):
                data[current_key] = []
            data[current_key].append(_parse_yaml_list_item(line[2:].strip()))
            continue
        if ":" not in line:
            raise ValueError(f"Unsupported YAML line: {line}")

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if indent > current_indent and current_key is not None:
            if not isinstance(data[current_key], dict):
                data[current_key] = {}
            data[current_key][key] = _parse_yaml_scalar(value)
            continue

        current_indent = indent
        if value:
            data[key] = _parse_yaml_scalar(value)
            current_key = None
        else:
            data[key] = []
            current_key = key
    return data


def _parse_yaml_list_item(value: str) -> Any:
    if ": " in value and not value.startswith(('"', "'", "{", "[")):
        key, item_value = value.split(":", 1)
        return {key.strip(): _parse_yaml_scalar(item_value.strip())}
    return _parse_yaml_scalar(value)


def _parse_yaml_scalar(value: str) -> Any:
    if value == "":
        return ""
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    if value.startswith(("{", "[")):
        return json.loads(value)
    if value.startswith(('"', "'")) and value.endswith(('"', "'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _value_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _bool_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() not in {"false", "0", "no", "off"}
    return bool(value)


def _format_example(example: Any) -> str:
    if isinstance(example, str):
        return f"- {example}"
    return "- " + json.dumps(example, ensure_ascii=False, sort_keys=True)
