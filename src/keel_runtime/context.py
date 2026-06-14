"""Context assembly and token budget management."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

TokenCounter = Callable[["Message"], int]


@dataclass(slots=True)
class Message:
    role: str
    content: Any
    name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.role.strip():
            raise ValueError("Message.role cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "name": self.name,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        return cls(
            role=str(data["role"]),
            content=data.get("content"),
            name=str(data["name"]) if data.get("name") is not None else None,
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(slots=True)
class ContextConfig:
    max_tokens: int
    keep_recent_turns: int = 10
    clear_consumed_results: bool = True
    compaction: str = "truncate"
    cache_control: bool = True
    token_counter: TokenCounter | None = None

    def __post_init__(self) -> None:
        if self.max_tokens <= 0:
            raise ValueError("ContextConfig.max_tokens must be positive")
        if self.keep_recent_turns < 0:
            raise ValueError("ContextConfig.keep_recent_turns cannot be negative")
        if self.compaction != "truncate":
            raise ValueError("Only compaction='truncate' is supported")


@dataclass(slots=True)
class ContextResult:
    messages: list[Message]
    tokens_used: int
    cache_breakpoints: list[int]
    trimmed_count: int = 0
    compaction_applied: str | None = None
    consumed_count: int = 0


class ContextProvider(Protocol):
    async def build_messages(
        self,
        system_prompt: str,
        history: Sequence[Message],
        new_messages: Sequence[Message],
        config: ContextConfig | None = None,
    ) -> ContextResult:
        """Build the final messages for a model call."""


@dataclass(slots=True)
class _PartitionedMessages:
    system: list[Message]
    task: list[Message]
    history: list[Message]
    active: list[Message]

    def flatten(self) -> list[Message]:
        return [*self.system, *self.task, *self.history, *self.active]


class PrefixStableContext:
    """Default context provider using stable prefixes and history truncation."""

    def __init__(
        self,
        *,
        max_tokens: int,
        keep_recent_turns: int = 10,
        clear_consumed_results: bool = True,
        compaction: str = "truncate",
        cache_control: bool = True,
        token_counter: TokenCounter | None = None,
        pressure_ratio: float = 0.70,
    ) -> None:
        self.default_config = ContextConfig(
            max_tokens=max_tokens,
            keep_recent_turns=keep_recent_turns,
            clear_consumed_results=clear_consumed_results,
            compaction=compaction,
            cache_control=cache_control,
            token_counter=token_counter,
        )
        if pressure_ratio < 0 or pressure_ratio > 1:
            raise ValueError("pressure_ratio must be between 0 and 1")
        self.pressure_ratio = pressure_ratio

    async def build_messages(
        self,
        system_prompt: str,
        history: Sequence[Message],
        new_messages: Sequence[Message],
        config: ContextConfig | None = None,
    ) -> ContextResult:
        cfg = config or self.default_config
        combined = self._copy_non_system_messages([*history, *new_messages])
        history_count = len(self._copy_non_system_messages(history))
        partitioned = self._partition(system_prompt, combined, history_count, cfg)
        tokens_used = self._count_tokens(partitioned.flatten(), cfg)
        consumed_count = 0
        operations: list[str] = []

        if (
            cfg.clear_consumed_results
            and tokens_used >= int(cfg.max_tokens * self.pressure_ratio)
        ):
            combined, consumed_count = self._clear_consumed_tool_results(combined)
            if consumed_count:
                operations.append("clear_consumed_results")
            partitioned = self._partition(system_prompt, combined, history_count, cfg)
            tokens_used = self._count_tokens(partitioned.flatten(), cfg)

        trimmed_count = 0
        while tokens_used > cfg.max_tokens and partitioned.history:
            partitioned.history.pop(0)
            trimmed_count += 1
            tokens_used = self._count_tokens(partitioned.flatten(), cfg)

        if trimmed_count:
            operations.append("truncate")

        messages = partitioned.flatten()
        return ContextResult(
            messages=messages,
            tokens_used=tokens_used,
            cache_breakpoints=self._cache_breakpoints(partitioned, cfg),
            trimmed_count=trimmed_count,
            compaction_applied="+".join(operations) if operations else None,
            consumed_count=consumed_count,
        )

    def _partition(
        self,
        system_prompt: str,
        messages: Sequence[Message],
        history_count: int,
        config: ContextConfig,
    ) -> _PartitionedMessages:
        system = (
            [
                Message(
                    role="system",
                    content=system_prompt,
                    metadata={"context_section": "system"},
                )
            ]
            if system_prompt
            else []
        )
        task_indices = self._task_indices(messages)
        active_indices = self._active_indices(messages, history_count, task_indices, config)

        task: list[Message] = []
        history: list[Message] = []
        active: list[Message] = []
        for index, message in enumerate(messages):
            if index in task_indices:
                task.append(self._with_section(message, "task"))
            elif index in active_indices:
                active.append(self._with_section(message, "active"))
            else:
                history.append(self._with_section(message, "history"))
        return _PartitionedMessages(system=system, task=task, history=history, active=active)

    def _task_indices(self, messages: Sequence[Message]) -> set[int]:
        first_user = next(
            (index for index, message in enumerate(messages) if message.role == "user"),
            None,
        )
        if first_user is None:
            return set()
        indices = {first_user}
        first_assistant = next(
            (
                index
                for index, message in enumerate(messages[first_user + 1 :], start=first_user + 1)
                if message.role == "assistant"
            ),
            None,
        )
        if first_assistant is not None:
            indices.add(first_assistant)
        return indices

    def _active_indices(
        self,
        messages: Sequence[Message],
        history_count: int,
        task_indices: set[int],
        config: ContextConfig,
    ) -> set[int]:
        active: set[int] = set(range(history_count, len(messages)))
        if config.keep_recent_turns > 0:
            user_indices = [
                index
                for index, message in enumerate(messages)
                if message.role == "user" and index not in task_indices
            ]
            if user_indices:
                recent_users = user_indices[-config.keep_recent_turns :]
                active_start = recent_users[0]
                active.update(
                    index
                    for index in range(active_start, len(messages))
                    if index not in task_indices
                )
            else:
                active.update(
                    index for index in range(len(messages)) if index not in task_indices
                )

        for index, message in enumerate(messages):
            if self._is_tool_result(message) and not self._has_later_assistant(messages, index):
                active.add(index)
        return active - task_indices

    def _clear_consumed_tool_results(
        self,
        messages: Sequence[Message],
    ) -> tuple[list[Message], int]:
        cleared: list[Message] = []
        consumed_count = 0
        for index, message in enumerate(messages):
            if self._is_tool_result(message) and self._has_later_assistant(messages, index):
                metadata = dict(message.metadata)
                metadata["consumed"] = True
                cleared.append(
                    Message(
                        role=message.role,
                        content="[consumed]",
                        name=message.name,
                        metadata=metadata,
                    )
                )
                consumed_count += 1
            else:
                cleared.append(self._copy_message(message))
        return cleared, consumed_count

    @staticmethod
    def _copy_non_system_messages(messages: Sequence[Message]) -> list[Message]:
        return [
            PrefixStableContext._copy_message(message)
            for message in messages
            if message.role != "system"
        ]

    @staticmethod
    def _copy_message(message: Message) -> Message:
        return Message(
            role=message.role,
            content=message.content,
            name=message.name,
            metadata=dict(message.metadata),
        )

    @staticmethod
    def _with_section(message: Message, section: str) -> Message:
        copied = PrefixStableContext._copy_message(message)
        copied.metadata["context_section"] = section
        return copied

    @staticmethod
    def _is_tool_result(message: Message) -> bool:
        return message.role == "tool" or bool(message.metadata.get("tool_result"))

    @staticmethod
    def _has_later_assistant(messages: Sequence[Message], index: int) -> bool:
        return any(message.role == "assistant" for message in messages[index + 1 :])

    @staticmethod
    def _count_tokens(messages: Sequence[Message], config: ContextConfig) -> int:
        counter = config.token_counter or default_token_counter
        return sum(max(0, int(counter(message))) for message in messages)

    @staticmethod
    def _cache_breakpoints(
        partitioned: _PartitionedMessages,
        config: ContextConfig,
    ) -> list[int]:
        if not config.cache_control:
            return []
        breakpoints: list[int] = []
        if partitioned.system:
            breakpoints.append(len(partitioned.system) - 1)
        if partitioned.task:
            breakpoints.append(len(partitioned.system) + len(partitioned.task) - 1)
        return breakpoints


def default_token_counter(message: Message) -> int:
    content = _stringify_content(message.content)
    token_estimate = (len(message.role) + len(content) + 3) // 4
    return max(1, token_estimate)


def _stringify_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(content)
