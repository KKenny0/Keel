"""Minimal single-agent loop orchestration."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from keel_runtime.context import ContextConfig, ContextProvider, ContextResult, Message
from keel_runtime.events import JobEvent, utc_now
from keel_runtime.gate import GateDecision, HumanGate
from keel_runtime.jobs import AgentLoopCheckpoint, AgentLoopCheckpointStatus
from keel_runtime.memory import MemoryProvider, memory_tools
from keel_runtime.output import parse_output
from keel_runtime.skills import AgentContext, ComposedPrompt, PromptComposer
from keel_runtime.tools import ToolCall, ToolRegistry, ToolResult

CheckpointSink = Callable[[AgentLoopCheckpoint], Awaitable[None] | None]
CheckpointSource = Callable[
    [],
    Awaitable[AgentLoopCheckpoint | None] | AgentLoopCheckpoint | None,
]


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
    prompt_composer: PromptComposer | None = None
    human_gate: HumanGate | None = None
    gate_tool_name: str = "human_gate"
    fail_on_gate_rejection: bool = False
    fail_on_gate_timeout: bool = True
    memory_provider: MemoryProvider | None = None
    memory_scope: str = "default"
    agent_name: str = "agent-loop"

    def __post_init__(self) -> None:
        if self.max_iterations <= 0:
            raise ValueError("AgentLoopConfig.max_iterations must be positive")
        if not self.job_id.strip():
            raise ValueError("AgentLoopConfig.job_id cannot be empty")
        if not self.gate_tool_name.strip():
            raise ValueError("AgentLoopConfig.gate_tool_name cannot be empty")
        if not self.memory_scope.strip():
            raise ValueError("AgentLoopConfig.memory_scope cannot be empty")
        if not self.agent_name.strip():
            raise ValueError("AgentLoopConfig.agent_name cannot be empty")


@dataclass(slots=True)
class AgentLoopResult:
    status: str
    output: Any = None
    raw_output: str | None = None
    error: str | None = None
    iterations: int = 0
    messages: list[Message] = field(default_factory=list)
    context_results: list[ContextResult] = field(default_factory=list)
    composed_prompts: list[ComposedPrompt] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    gate_decisions: list[GateDecision] = field(default_factory=list)
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
        self.config = config or AgentLoopConfig()
        self.tools = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)
        if self.config.memory_provider is not None:
            for spec in memory_tools(
                self.config.memory_provider,
                default_scope=self.config.memory_scope,
            ):
                if self.tools.get(spec.name) is None:
                    self.tools.register(spec)
        if (
            self.config.human_gate is not None
            and self.tools.get(self.config.gate_tool_name) is None
        ):
            self.tools.register(
                self.config.human_gate.as_tool(
                    name=self.config.gate_tool_name,
                    job_id=self.config.job_id,
                )
            )

    async def run(
        self,
        input: str | Message | Sequence[Message | dict[str, Any]],
        *,
        history: Sequence[Message | dict[str, Any]] | None = None,
        agent_context: AgentContext | dict[str, Any] | None = None,
        attempt_id: str = "direct",
        checkpoint_sink: CheckpointSink | None = None,
        checkpoint_source: CheckpointSource | AgentLoopCheckpoint | None = None,
    ) -> AgentLoopResult:
        if not attempt_id.strip():
            raise ValueError("attempt_id cannot be empty")
        events: list[JobEvent] = []
        context_results: list[ContextResult] = []
        composed_prompts: list[ComposedPrompt] = []
        tool_results: list[ToolResult] = []
        gate_decisions: list[GateDecision] = []
        history_messages = _normalize_message_sequence(history or [])
        active_messages = _normalize_loop_input(input)
        base_agent_context = _normalize_agent_context(agent_context, active_messages)
        pending_tool_calls: list[ToolCall] = []
        start_iteration = 1

        checkpoint = await self._load_checkpoint(checkpoint_source)
        if checkpoint is not None:
            self._validate_checkpoint(checkpoint)
            history_messages = [
                Message.from_dict(item) for item in checkpoint.history_messages
            ]
            active_messages = [
                Message.from_dict(item) for item in checkpoint.active_messages
            ]
            context_results = [
                _context_result_from_dict(item) for item in checkpoint.context_results
            ]
            composed_prompts = [
                _composed_prompt_from_dict(item) for item in checkpoint.composed_prompts
            ]
            tool_results = [
                ToolResult.from_dict(item) for item in checkpoint.completed_tool_results
            ]
            gate_decisions = [
                GateDecision.from_dict(item) for item in checkpoint.gate_decisions
            ]
            pending_tool_calls = [
                ToolCall.from_dict(item) for item in checkpoint.pending_tool_calls
            ]
            base_agent_context = _normalize_agent_context(agent_context, active_messages)
            start_iteration = checkpoint.iteration + 1
            events.append(
                self._event(
                    "agent loop resumed from checkpoint",
                    iteration=checkpoint.iteration,
                    checkpoint_status=checkpoint.status.value,
                )
            )
            if checkpoint.status == AgentLoopCheckpointStatus.COMPLETED:
                return AgentLoopResult(
                    status="succeeded",
                    output=checkpoint.parsed_output,
                    raw_output=checkpoint.raw_output,
                    iterations=checkpoint.iteration,
                    messages=[*history_messages, *active_messages],
                    context_results=context_results,
                    composed_prompts=composed_prompts,
                    tool_results=tool_results,
                    gate_decisions=gate_decisions,
                    events=events,
                )
            if checkpoint.status == AgentLoopCheckpointStatus.FAILED:
                error = (
                    str(checkpoint.parse_error.get("message"))
                    if checkpoint.parse_error and checkpoint.parse_error.get("message")
                    else "agent loop checkpoint failed"
                )
                return AgentLoopResult(
                    status="failed",
                    error=error,
                    iterations=checkpoint.iteration,
                    messages=[*history_messages, *active_messages],
                    context_results=context_results,
                    composed_prompts=composed_prompts,
                    tool_results=tool_results,
                    gate_decisions=gate_decisions,
                    events=events,
                )

        events.append(self._event("agent loop started", status="running"))
        await self._save_checkpoint(
            checkpoint_sink,
            attempt_id=attempt_id,
            iteration=max(start_iteration - 1, 0),
            status=AgentLoopCheckpointStatus.RUNNING,
            history_messages=history_messages,
            active_messages=active_messages,
            pending_tool_calls=pending_tool_calls,
            context_results=context_results,
            composed_prompts=composed_prompts,
            tool_results=tool_results,
            gate_decisions=gate_decisions,
        )

        if pending_tool_calls:
            terminal_result = await self._execute_tool_calls(
                pending_tool_calls,
                iteration=max(start_iteration - 1, 0),
                history_messages=history_messages,
                active_messages=active_messages,
                context_results=context_results,
                composed_prompts=composed_prompts,
                tool_results=tool_results,
                gate_decisions=gate_decisions,
                events=events,
                checkpoint_sink=checkpoint_sink,
                attempt_id=attempt_id,
            )
            if terminal_result is not None:
                return terminal_result

        for iteration in range(start_iteration, self.config.max_iterations + 1):
            system_prompt, composed_prompt = await self._compose_system_prompt(
                base_agent_context,
                history_messages,
                active_messages,
            )
            if composed_prompt is not None:
                composed_prompts.append(composed_prompt)
            context = await self.context_provider.build_messages(
                system_prompt,
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
                await self._save_checkpoint(
                    checkpoint_sink,
                    attempt_id=attempt_id,
                    iteration=iteration,
                    status=AgentLoopCheckpointStatus.AWAITING_TOOL,
                    history_messages=history_messages,
                    active_messages=active_messages,
                    pending_tool_calls=response.tool_calls,
                    context_results=context_results,
                    composed_prompts=composed_prompts,
                    tool_results=tool_results,
                    gate_decisions=gate_decisions,
                )
                terminal_result = await self._execute_tool_calls(
                    response.tool_calls,
                    iteration=iteration,
                    history_messages=history_messages,
                    active_messages=active_messages,
                    context_results=context_results,
                    composed_prompts=composed_prompts,
                    tool_results=tool_results,
                    gate_decisions=gate_decisions,
                    events=events,
                    checkpoint_sink=checkpoint_sink,
                    attempt_id=attempt_id,
                )
                if terminal_result is not None:
                    return terminal_result
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
            await self._save_checkpoint(
                checkpoint_sink,
                attempt_id=attempt_id,
                iteration=iteration,
                status=AgentLoopCheckpointStatus.COMPLETED,
                history_messages=history_messages,
                active_messages=active_messages,
                pending_tool_calls=[],
                context_results=context_results,
                composed_prompts=composed_prompts,
                tool_results=tool_results,
                gate_decisions=gate_decisions,
                raw_output=raw_output,
                parsed_output=output,
            )
            return AgentLoopResult(
                status="succeeded",
                output=output,
                raw_output=raw_output,
                iterations=iteration,
                messages=[*history_messages, *active_messages],
                context_results=context_results,
                composed_prompts=composed_prompts,
                tool_results=tool_results,
                gate_decisions=gate_decisions,
                events=events,
            )

        error = f"Maximum iterations reached: {self.config.max_iterations}"
        events.append(JobEvent.error(self.config.job_id, error, status="max_iterations"))
        await self._save_checkpoint(
            checkpoint_sink,
            attempt_id=attempt_id,
            iteration=self.config.max_iterations,
            status=AgentLoopCheckpointStatus.FAILED,
            history_messages=history_messages,
            active_messages=active_messages,
            pending_tool_calls=[],
            context_results=context_results,
            composed_prompts=composed_prompts,
            tool_results=tool_results,
            gate_decisions=gate_decisions,
            parse_error={"code": "max_iterations", "message": error},
        )
        return AgentLoopResult(
            status="max_iterations",
            error=error,
            iterations=self.config.max_iterations,
            messages=[*history_messages, *active_messages],
            context_results=context_results,
            composed_prompts=composed_prompts,
            tool_results=tool_results,
            gate_decisions=gate_decisions,
            events=events,
        )

    async def _chat(self, messages: Sequence[Message]) -> _ChatResponse:
        response = self.client.chat(messages, self.tools.to_list())
        if inspect.isawaitable(response):
            response = await response
        return _normalize_chat_response(response)

    def _event(self, message: str, **data: Any) -> JobEvent:
        return JobEvent.status(self.config.job_id, message, **data)

    def _drain_gate_events(self) -> list[JobEvent]:
        if self.config.human_gate is None:
            return []
        return self.config.human_gate.drain_events()

    async def _execute_tool_calls(
        self,
        calls: Sequence[ToolCall],
        *,
        iteration: int,
        history_messages: Sequence[Message],
        active_messages: list[Message],
        context_results: list[ContextResult],
        composed_prompts: list[ComposedPrompt],
        tool_results: list[ToolResult],
        gate_decisions: list[GateDecision],
        events: list[JobEvent],
        checkpoint_sink: CheckpointSink | None,
        attempt_id: str,
    ) -> AgentLoopResult | None:
        pending = list(calls)
        while pending:
            call = pending.pop(0)
            events.append(
                self._event(
                    "tool call started",
                    tool_name=call.name,
                    call_id=call.call_id,
                )
            )
            result = await self.tools.execute(call)
            tool_results.append(result)
            events.extend(self._drain_gate_events())
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
            gate_decision = self._gate_decision(call, result)
            if gate_decision is not None:
                gate_decisions.append(gate_decision)

            await self._save_checkpoint(
                checkpoint_sink,
                attempt_id=attempt_id,
                iteration=iteration,
                status=(
                    AgentLoopCheckpointStatus.AWAITING_TOOL
                    if pending
                    else AgentLoopCheckpointStatus.RUNNING
                ),
                history_messages=history_messages,
                active_messages=active_messages,
                pending_tool_calls=pending,
                context_results=context_results,
                composed_prompts=composed_prompts,
                tool_results=tool_results,
                gate_decisions=gate_decisions,
            )

            if gate_decision is not None:
                gated_result = self._gate_terminal_result(
                    gate_decision,
                    iteration,
                    history_messages,
                    active_messages,
                    context_results,
                    composed_prompts,
                    tool_results,
                    gate_decisions,
                    events,
                )
                if gated_result is not None:
                    await self._save_checkpoint(
                        checkpoint_sink,
                        attempt_id=attempt_id,
                        iteration=iteration,
                        status=AgentLoopCheckpointStatus.FAILED,
                        history_messages=history_messages,
                        active_messages=active_messages,
                        pending_tool_calls=pending,
                        context_results=context_results,
                        composed_prompts=composed_prompts,
                        tool_results=tool_results,
                        gate_decisions=gate_decisions,
                        parse_error={
                            "code": gated_result.status,
                            "message": gated_result.error or gated_result.status,
                        },
                    )
                    return gated_result

            if result.ok:
                events.append(
                    self._event(
                        "tool call completed",
                        tool_name=result.name,
                        call_id=result.call_id,
                    )
                )
                continue

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
                await self._save_checkpoint(
                    checkpoint_sink,
                    attempt_id=attempt_id,
                    iteration=iteration,
                    status=AgentLoopCheckpointStatus.FAILED,
                    history_messages=history_messages,
                    active_messages=active_messages,
                    pending_tool_calls=pending,
                    context_results=context_results,
                    composed_prompts=composed_prompts,
                    tool_results=tool_results,
                    gate_decisions=gate_decisions,
                    parse_error={
                        "code": "tool_error",
                        "message": result.error or "tool call failed",
                    },
                )
                return AgentLoopResult(
                    status="failed",
                    error=result.error,
                    iterations=iteration,
                    messages=[*history_messages, *active_messages],
                    context_results=context_results,
                    composed_prompts=composed_prompts,
                    tool_results=tool_results,
                    gate_decisions=gate_decisions,
                    events=events,
                )
        return None

    async def _load_checkpoint(
        self,
        checkpoint_source: CheckpointSource | AgentLoopCheckpoint | None,
    ) -> AgentLoopCheckpoint | None:
        if checkpoint_source is None:
            return None
        if isinstance(checkpoint_source, AgentLoopCheckpoint):
            return checkpoint_source
        checkpoint = checkpoint_source()
        if inspect.isawaitable(checkpoint):
            checkpoint = await checkpoint
        return checkpoint

    async def _save_checkpoint(
        self,
        checkpoint_sink: CheckpointSink | None,
        *,
        attempt_id: str,
        iteration: int,
        status: AgentLoopCheckpointStatus,
        history_messages: Sequence[Message],
        active_messages: Sequence[Message],
        pending_tool_calls: Sequence[ToolCall],
        context_results: Sequence[ContextResult],
        composed_prompts: Sequence[ComposedPrompt],
        tool_results: Sequence[ToolResult],
        gate_decisions: Sequence[GateDecision],
        raw_output: str | None = None,
        parsed_output: Any = None,
        parse_error: dict[str, Any] | None = None,
    ) -> None:
        if checkpoint_sink is None:
            return
        now = utc_now()
        checkpoint = AgentLoopCheckpoint(
            version=1,
            job_id=self.config.job_id,
            attempt_id=attempt_id,
            agent_name=self.config.agent_name,
            iteration=iteration,
            status=status,
            history_messages=[message.to_dict() for message in history_messages],
            active_messages=[message.to_dict() for message in active_messages],
            pending_tool_calls=[call.to_dict() for call in pending_tool_calls],
            completed_tool_results=[result.to_dict() for result in tool_results],
            context_results=[
                _context_result_to_dict(result) for result in context_results
            ],
            composed_prompts=[
                _composed_prompt_to_dict(prompt) for prompt in composed_prompts
            ],
            gate_decisions=[decision.to_dict() for decision in gate_decisions],
            raw_output=raw_output,
            parsed_output=parsed_output,
            parse_error=parse_error,
            created_at=now,
            updated_at=now,
        )
        result = checkpoint_sink(checkpoint)
        if inspect.isawaitable(result):
            await result

    def _validate_checkpoint(self, checkpoint: AgentLoopCheckpoint) -> None:
        if checkpoint.job_id != self.config.job_id:
            raise ValueError("checkpoint job_id does not match AgentLoopConfig.job_id")

    def _gate_decision(
        self,
        call: ToolCall,
        result: ToolResult,
    ) -> GateDecision | None:
        if call.name != self.config.gate_tool_name or not result.ok:
            return None
        return _gate_decision_from_output(result.output)

    def _gate_terminal_result(
        self,
        decision: GateDecision,
        iteration: int,
        history_messages: Sequence[Message],
        active_messages: Sequence[Message],
        context_results: list[ContextResult],
        composed_prompts: list[ComposedPrompt],
        tool_results: list[ToolResult],
        gate_decisions: list[GateDecision],
        events: list[JobEvent],
    ) -> AgentLoopResult | None:
        if decision.rejected and self.config.fail_on_gate_rejection:
            error = decision.feedback or "human gate rejected"
            events.append(JobEvent.error(self.config.job_id, error, status="gate_rejected"))
            return AgentLoopResult(
                status="gate_rejected",
                error=error,
                iterations=iteration,
                messages=[*history_messages, *active_messages],
                context_results=context_results,
                composed_prompts=composed_prompts,
                tool_results=tool_results,
                gate_decisions=gate_decisions,
                events=events,
            )
        if decision.timed_out and self.config.fail_on_gate_timeout:
            error = decision.feedback or "human gate timed out"
            events.append(JobEvent.error(self.config.job_id, error, status="gate_timeout"))
            return AgentLoopResult(
                status="gate_timeout",
                error=error,
                iterations=iteration,
                messages=[*history_messages, *active_messages],
                context_results=context_results,
                composed_prompts=composed_prompts,
                tool_results=tool_results,
                gate_decisions=gate_decisions,
                events=events,
            )
        return None

    async def _compose_system_prompt(
        self,
        base_context: AgentContext,
        history_messages: Sequence[Message],
        active_messages: Sequence[Message],
    ) -> tuple[str, ComposedPrompt | None]:
        composer = self.config.prompt_composer
        if composer is None:
            return self.config.system_prompt, None
        context = AgentContext(
            task=base_context.task,
            metadata=dict(base_context.metadata),
            history_count=len(history_messages),
            active_count=len(active_messages),
        )
        composed = composer.compose(self.config.system_prompt, context)
        if inspect.isawaitable(composed):
            composed = await composed
        return composed.content, composed


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


def _normalize_agent_context(
    agent_context: AgentContext | dict[str, Any] | None,
    active_messages: Sequence[Message],
) -> AgentContext:
    if isinstance(agent_context, AgentContext):
        task = agent_context.task or _task_from_messages(active_messages)
        return AgentContext(task=task, metadata=dict(agent_context.metadata))
    if isinstance(agent_context, dict):
        context = AgentContext.from_dict(agent_context)
        context.task = context.task or _task_from_messages(active_messages)
        return context
    return AgentContext(task=_task_from_messages(active_messages))


def _task_from_messages(messages: Sequence[Message]) -> str:
    first_user = next((message for message in messages if message.role == "user"), None)
    return _content_to_text(first_user.content) if first_user is not None else ""


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


def _gate_decision_from_output(output: Any) -> GateDecision | None:
    if isinstance(output, GateDecision):
        return output
    if not isinstance(output, dict) or "status" not in output or "request_id" not in output:
        return None
    try:
        return GateDecision.from_dict(output)
    except (KeyError, TypeError, ValueError):
        return None


def _context_result_to_dict(result: ContextResult) -> dict[str, Any]:
    return {
        "messages": [message.to_dict() for message in result.messages],
        "tokens_used": result.tokens_used,
        "cache_breakpoints": list(result.cache_breakpoints),
        "trimmed_count": result.trimmed_count,
        "compaction_applied": result.compaction_applied,
        "consumed_count": result.consumed_count,
    }


def _context_result_from_dict(data: dict[str, Any]) -> ContextResult:
    return ContextResult(
        messages=[Message.from_dict(item) for item in data.get("messages") or []],
        tokens_used=int(data.get("tokens_used") or 0),
        cache_breakpoints=list(data.get("cache_breakpoints") or []),
        trimmed_count=int(data.get("trimmed_count") or 0),
        compaction_applied=(
            str(data["compaction_applied"])
            if data.get("compaction_applied") is not None
            else None
        ),
        consumed_count=int(data.get("consumed_count") or 0),
    )


def _composed_prompt_to_dict(prompt: ComposedPrompt) -> dict[str, Any]:
    return {
        "content": prompt.content,
        "skill_names": list(prompt.skill_names),
        "constraints": list(prompt.constraints),
        "examples": list(prompt.examples),
        "schema_overrides": dict(prompt.schema_overrides),
        "metadata": dict(prompt.metadata),
    }


def _composed_prompt_from_dict(data: dict[str, Any]) -> ComposedPrompt:
    return ComposedPrompt(
        content=str(data.get("content") or ""),
        skill_names=[str(item) for item in data.get("skill_names") or []],
        constraints=[str(item) for item in data.get("constraints") or []],
        examples=list(data.get("examples") or []),
        schema_overrides=dict(data.get("schema_overrides") or {}),
        metadata=dict(data.get("metadata") or {}),
    )
