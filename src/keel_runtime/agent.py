"""Small decorator API for assembling a runnable Keel agent."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from functools import update_wrapper
from typing import Any

from keel_runtime.context import ContextConfig, ContextProvider, Message, PrefixStableContext
from keel_runtime.gate import HumanGate
from keel_runtime.loop import AgentLoop, AgentLoopConfig, AgentLoopResult, ChatClient
from keel_runtime.memory import MemoryProvider
from keel_runtime.skills import PromptComposer
from keel_runtime.tools import ToolHandler, ToolRegistry, ToolSpec

AgentInput = str | Message | Sequence[Message | dict[str, Any]]
AgentInputBuilder = Callable[..., Awaitable[Any] | Any]
AgentTools = ToolRegistry | Sequence[ToolSpec | ToolHandler] | None


class Agent:
    """Callable wrapper returned by :func:`agent`."""

    def __init__(
        self,
        handler: AgentInputBuilder,
        *,
        client: ChatClient,
        context: ContextProvider | None = None,
        tools: AgentTools = None,
        system_prompt: str = "",
        max_iterations: int = 8,
        max_tokens: int = 16_000,
        context_config: ContextConfig | None = None,
        composer: PromptComposer | None = None,
        memory: MemoryProvider | None = None,
        memory_scope: str = "default",
        human_gate: HumanGate | None = None,
        gate_tool_name: str = "human_gate",
        output_model: Any | None = None,
        parse_final_output: bool = True,
        fail_on_tool_error: bool = False,
        fail_on_gate_rejection: bool = False,
        fail_on_gate_timeout: bool = True,
        job_id: str | None = None,
    ) -> None:
        self.handler = handler
        self.client = client
        self.context_provider = context or PrefixStableContext(max_tokens=max_tokens)
        self.tools = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)
        self.config = AgentLoopConfig(
            system_prompt=system_prompt,
            max_iterations=max_iterations,
            context_config=context_config,
            parse_final_output=parse_final_output,
            output_model=output_model,
            fail_on_tool_error=fail_on_tool_error,
            job_id=job_id or _handler_name(handler),
            prompt_composer=composer,
            human_gate=human_gate,
            gate_tool_name=gate_tool_name,
            fail_on_gate_rejection=fail_on_gate_rejection,
            fail_on_gate_timeout=fail_on_gate_timeout,
            memory_provider=memory,
            memory_scope=memory_scope,
            agent_name=_handler_name(handler),
        )
        update_wrapper(self, handler)

    async def __call__(self, *args: Any, **kwargs: Any) -> AgentLoopResult:
        return await self.loop().run(await self.build_input(*args, **kwargs))

    async def build_input(self, *args: Any, **kwargs: Any) -> AgentInput:
        """Build normalized loop input without executing the loop."""

        built_input = self.handler(*args, **kwargs)
        if inspect.isawaitable(built_input):
            built_input = await built_input
        return _normalize_agent_input(built_input)

    def loop(self) -> AgentLoop:
        """Create a lower-level AgentLoop with this agent's defaults."""

        return AgentLoop(
            self.client,
            self.context_provider,
            self.tools,
            self.config,
        )


def agent(
    _func: AgentInputBuilder | None = None,
    *,
    client: ChatClient,
    context: ContextProvider | None = None,
    tools: AgentTools = None,
    system_prompt: str = "",
    max_iterations: int = 8,
    max_tokens: int = 16_000,
    context_config: ContextConfig | None = None,
    composer: PromptComposer | None = None,
    memory: MemoryProvider | None = None,
    memory_scope: str = "default",
    human_gate: HumanGate | None = None,
    gate_tool_name: str = "human_gate",
    output_model: Any | None = None,
    parse_final_output: bool = True,
    fail_on_tool_error: bool = False,
    fail_on_gate_rejection: bool = False,
    fail_on_gate_timeout: bool = True,
    job_id: str | None = None,
) -> Callable[[AgentInputBuilder], Agent] | Agent:
    """Wrap a function that builds agent input into a runnable Keel agent."""

    def decorator(func: AgentInputBuilder) -> Agent:
        return Agent(
            func,
            client=client,
            context=context,
            tools=tools,
            system_prompt=system_prompt,
            max_iterations=max_iterations,
            max_tokens=max_tokens,
            context_config=context_config,
            composer=composer,
            memory=memory,
            memory_scope=memory_scope,
            human_gate=human_gate,
            gate_tool_name=gate_tool_name,
            output_model=output_model,
            parse_final_output=parse_final_output,
            fail_on_tool_error=fail_on_tool_error,
            fail_on_gate_rejection=fail_on_gate_rejection,
            fail_on_gate_timeout=fail_on_gate_timeout,
            job_id=job_id,
        )

    if _func is None:
        return decorator
    return decorator(_func)


def _normalize_agent_input(value: Any) -> AgentInput:
    if isinstance(value, str | Message):
        return value
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        if all(isinstance(item, Message | dict) for item in value):
            return value
    return Message(role="user", content=value)


def _handler_name(handler: AgentInputBuilder) -> str:
    name = getattr(handler, "__name__", None) or handler.__class__.__name__
    return str(name).replace("_", "-")
