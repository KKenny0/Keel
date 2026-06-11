"""Minimal single-agent loop orchestration."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from keel_runtime.context import ContextConfig, ContextProvider, ContextResult, Message
from keel_runtime.events import JobEvent
from keel_runtime.output import parse_output
from keel_runtime.tools import ToolCall, ToolRegistry, ToolResult


class ChatClient(Protocol):
    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> Awaitable[Any]:
        """Return a model response for the provided messages and tools."""


@dataclass(slots=True)
class AgentLoopConfig:
    system_prompt: str = ""
    max_iterations: int = 8
    context_config: ContextConfig | None = None
    parse_final_output: bool = True
    output_model: Any | None = None
    fail_on_tool_error: bool = False
    job_id: str = "agent-loop"

    def __post_init__(self) -> None:
        if self.max_iterations <= 0:
            raise ValueError("AgentLoopConfig.max_iterations must be positive")
        if not self.job_id.strip():
            raise ValueError("AgentLoopConfig.job_id cannot be empty")


@dataclass(slots=True)
class AgentLoopResult:
    status: str
    output: Any = None
    raw_output: str | None = None
    error: str | None = None
    iterations: int = 0
    messages: list[Message] = field(default_factory=list)
    context_results: list[ContextResult] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    events: list[JobEvent] = field(default_factory=list)


@dataclass(slots=True)
class _ChatResponse:
    content: Any = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] | None = None


class AgentLoop:
    def __init__(
        self,
        client: ChatClient,
        context_provider: ContextProvider,
        tools: ToolRegistry | Sequence[Any] | None = None,
        config: AgentLoopConfig | None = None,
    ) -> None:
        self.client = client
        self.context_provider = context_provider
        self.tools = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)
        self.config = config or AgentLoopConfig()

    async def run(
        self,
        input: str | Message | Sequence[Message | dict[str, Any]],
        *,
        history: Sequence[Message | dict[str, Any]] | None = None,
    ) -> AgentLoopResult:
        events: list[JobEvent] = []
        context_results: list[ContextResult] = []
        tool_results: list[ToolResult] = []
        history_messages = _normalize_message_sequence(history or [])
        active_messages = _normalize_loop_input(input)

        events.append(self._event("agent loop started", status="running"))
        for iteration in range(1, self.config.max_iterations + 1):
            context = await self.context_provider.build_messages(
                self.config.system_prompt,
                history_messages,
                active_messages,
                self.config.context_config,
            )
            context_results.append(context)
            events.append(
                self._event(
                    "agent loop iteration",
                    iteration=iteration,
                    tokens_used=context.tokens_used,
                    trimmed_count=context.trimmed_count,
                    consumed_count=context.consumed_count,
                )
            )

            response = await self._chat(context.messages)
            if response.usage is not None:
                events.append(self._event("agent loop usage recorded", usage=response.usage))

            if response.tool_calls:
                active_messages.append(
                    Message(
                        role="assistant",
                        content=_content_to_text(response.content),
                        metadata={
                            "tool_calls": [call.to_dict() for call in response.tool_calls],
                        },
                    )
                )
                for call in response.tool_calls:
                    events.append(
                        self._event(
                            "tool call started",
                            tool_name=call.name,
                            call_id=call.call_id,
                        )
                    )
                    result = await self.tools.execute(call)
                    tool_results.append(result)
                    active_messages.append(
                        Message(
                            role="tool",
                            content=result.to_dict(),
                            name=result.name,
                            metadata={
                                "tool_result": True,
                                "tool_call_id": result.call_id,
                            },
                        )
                    )
                    if result.ok:
                        events.append(
                            self._event(
                                "tool call completed",
                                tool_name=result.name,
                                call_id=result.call_id,
                            )
                        )
                    else:
                        events.append(
                            JobEvent.error(
                                self.config.job_id,
                                "tool call failed",
                                tool_name=result.name,
                                call_id=result.call_id,
                                error=result.error,
                            )
                        )
                        if self.config.fail_on_tool_error:
                            return AgentLoopResult(
                                status="failed",
                                error=result.error,
                                iterations=iteration,
                                messages=[*history_messages, *active_messages],
                                context_results=context_results,
                                tool_results=tool_results,
                                events=events,
                            )
                continue

            raw_output = _content_to_text(response.content)
            active_messages.append(Message(role="assistant", content=raw_output))
            output = (
                parse_output(raw_output, self.config.output_model)
                if self.config.parse_final_output
                else raw_output
            )
            events.append(JobEvent.output(self.config.job_id, raw_output))
            events.append(self._event("agent loop completed", status="succeeded"))
            return AgentLoopResult(
                status="succeeded",
                output=output,
                raw_output=raw_output,
                iterations=iteration,
                messages=[*history_messages, *active_messages],
                context_results=context_results,
                tool_results=tool_results,
                events=events,
            )

        error = f"Maximum iterations reached: {self.config.max_iterations}"
        events.append(JobEvent.error(self.config.job_id, error, status="max_iterations"))
        return AgentLoopResult(
            status="max_iterations",
            error=error,
            iterations=self.config.max_iterations,
            messages=[*history_messages, *active_messages],
            context_results=context_results,
            tool_results=tool_results,
            events=events,
        )

    async def _chat(self, messages: Sequence[Message]) -> _ChatResponse:
        response = self.client.chat(messages, self.tools.to_list())
        if inspect.isawaitable(response):
            response = await response
        return _normalize_chat_response(response)

    def _event(self, message: str, **data: Any) -> JobEvent:
        return JobEvent.status(self.config.job_id, message, **data)


def _normalize_loop_input(
    input: str | Message | Sequence[Message | dict[str, Any]],
) -> list[Message]:
    if isinstance(input, str):
        return [Message(role="user", content=input)]
    if isinstance(input, Message):
        return [_copy_message(input)]
    return _normalize_message_sequence(input)


def _normalize_message_sequence(
    messages: Sequence[Message | dict[str, Any]],
) -> list[Message]:
    return [_normalize_message(message) for message in messages]


def _normalize_message(message: Message | dict[str, Any]) -> Message:
    if isinstance(message, Message):
        return _copy_message(message)
    return Message.from_dict(message)


def _copy_message(message: Message) -> Message:
    return Message(
        role=message.role,
        content=message.content,
        name=message.name,
        metadata=dict(message.metadata),
    )


def _normalize_chat_response(response: Any) -> _ChatResponse:
    if isinstance(response, str):
        return _ChatResponse(content=response)
    content = _field(response, "content", "")
    tool_calls = [_normalize_tool_call(call) for call in _field(response, "tool_calls", []) or []]
    usage = _field(response, "usage", None)
    return _ChatResponse(
        content=content,
        tool_calls=tool_calls,
        usage=dict(usage) if isinstance(usage, dict) else None,
    )


def _normalize_tool_call(call: ToolCall | dict[str, Any]) -> ToolCall:
    if isinstance(call, ToolCall):
        return call
    data = dict(call)
    if "function" in data and "name" not in data:
        function = dict(data["function"] or {})
        return ToolCall(
            name=str(function["name"]),
            arguments=_normalize_arguments(function.get("arguments")),
            call_id=str(data["id"]) if data.get("id") is not None else None,
        )
    return ToolCall(
        name=str(data["name"]),
        arguments=_normalize_arguments(data.get("arguments")),
        call_id=(
            str(data["call_id"])
            if data.get("call_id") is not None
            else str(data["id"]) if data.get("id") is not None else None
        ),
    )


def _normalize_arguments(arguments: Any) -> dict[str, Any]:
    if arguments is None:
        return {}
    if isinstance(arguments, str):
        parsed = json.loads(arguments)
        if not isinstance(parsed, dict):
            raise TypeError("tool call arguments JSON must decode to an object")
        return parsed
    if not isinstance(arguments, dict):
        raise TypeError("tool call arguments must be a dict")
    return dict(arguments)


def _field(response: Any, name: str, default: Any) -> Any:
    if isinstance(response, dict):
        return response.get(name, default)
    return getattr(response, name, default)


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(content)
