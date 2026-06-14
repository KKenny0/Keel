<p align="center">
  <img src="design/keel-logo-wordmark.svg" alt="Keel" width="260">
</p>

<p align="center">
  Embeddable Agent Runtime Toolkit<br>
  Job lifecycle, context management, tool protocol, structured output, persistent recovery, out of the box.
</p>

---

## What is Keel

Keel is a Python toolkit. It embeds into your project and gives your agents production-grade runtime capabilities: agent loop, context management, tool execution, structured output parsing, job persistence and recovery. You write the domain-specific parts (system prompt, output type, business tools). Keel handles the rest.

```text
pip install keel-runtime
```

The integration model is not "deploy a Keel service, then register agents." It's "add a few lines of code, and your code gains production capabilities."

## Building Blocks

| Module | Responsibility | Status |
| --- | --- | --- |
| `AgentLoop` | LLM calls, tool execution, iteration control, usage reporting | done |
| `PrefixStableContext` | Token budget, stable prefix partitioning, consumed tool result cleanup, history trimming | done |
| `ToolRegistry` / `@tool` | Decorator-based tool definition, auto-generated schema, registration and execution | done |
| `parse_output` / `extract_json` | JSON extraction from LLM text, Pydantic validation, text fallback | done |
| `InProcessRuntime` | Wrap a Python async callable as a Keel job | done |
| `JobManager` | Create, run, stop, resume/retry/replay/restore, query jobs and collaborations | done |
| `ModelConfig` | Structured model config, declarative fallback, provider validation | done |
| `AgentSpec` / `AgentJob` | Agent definition, job status, dependencies, resource limits | done |
| `stores` | Local filesystem + S3/MinIO persistence | done |
| `events` | Streaming event system | done |
| `collaboration` | Multi-agent collaboration, serial/parallel, human confirmation, retry | done |
| `@agent` / `Agent` | Minimal decorator entry point for client, context, tools, memory, and gates | done |
| `PromptComposer` | Skill injection protocol | done |
| `HumanGate` | Standalone confirmation primitive | done |
| `MemoryProvider` / `LocalMemoryProvider` | Memory access protocol and local JSONL implementation | done |

## Quick Start

The 5-minute path: define a tool, wrap a function with `@keel.agent`, then call
that function directly. This example uses a mock client, so no real LLM is
required. A runnable version lives at `examples/quickstart_agent.py`.

```python
import asyncio
from typing import Any

import keel_runtime as keel


@keel.tool(name="get_weather", description="Get current weather for a city")
def get_weather(city: str) -> str:
    return f"{city}: 22C, sunny"


class MockClient:
    async def chat(self, messages, tools) -> dict[str, Any] | str:
        tool_names = {message.name for message in messages if message.role == "tool"}
        if "memory_record" not in tool_names:
            return {
                "tool_calls": [
                    {
                        "name": "memory_record",
                        "arguments": {
                            "title": "Weather quickstart",
                            "outcome": "Use get_weather for forecast questions",
                            "tags": ["quickstart"],
                        },
                    }
                ]
            }
        if "get_weather" not in tool_names:
            return {
                "tool_calls": [
                    {"name": "get_weather", "arguments": {"city": "Tokyo"}}
                ]
            }

        weather = next(
            message.content["output"]
            for message in messages
            if message.role == "tool" and message.name == "get_weather"
        )
        return f"Weather report: {weather}"


memory = keel.LocalMemoryProvider()


@keel.agent(
    client=MockClient(),
    tools=[get_weather],
    memory=memory,
    memory_scope="quickstart",
    system_prompt="You are a concise weather assistant.",
    max_iterations=5,
)
async def weather_agent(question: str) -> str:
    return question


async def main():
    result = await weather_agent("What is the weather in Tokyo?")
    print(result.status)      # succeeded
    print(result.output)      # Weather report: Tokyo: 22C, sunny
    print(memory.list_decisions(scope="quickstart")[0].title)

    manager = keel.JobManager(root=".keel")
    job_id = manager.create_agent_job(weather_agent, "What is the weather in Tokyo?")
    async for event in manager.stream(job_id):
        print(event.message)


asyncio.run(main())
```

You can also run the repository example directly:

```bash
python examples/quickstart_agent.py
```

Use the lower-level `AgentLoop` API below when you need finer control over
iterations, history, or job ids.

## Agent Loop

`AgentLoop` wires together LLM calls, ContextProvider assembly, tool execution, and structured output parsing into a complete cycle.

Your LLM client only needs to satisfy the minimal signature `async def chat(messages, tools) -> response`:

```python
class MyClient:
    async def chat(self, messages, tools):
        response = await openai_client.chat.completions.create(
            model="gpt-4.1",
            messages=[m.to_dict() for m in messages],
            tools=[{"type": "function", "function": t} for t in tools],
        )
        return response.choices[0].message
```

Configuration:

```python
config = AgentLoopConfig(
    system_prompt="You are a research assistant.",
    max_iterations=10,
    parse_final_output=True,       # Auto-parse final output as JSON
    output_model=MyPydanticModel,  # Optional: validate with Pydantic
    output_mode="strict",          # Optional: fail on invalid typed output
    fail_on_tool_error=False,      # Whether to abort on tool failure
    job_id="my-agent-job",
)
```

`AgentLoopResult` contains:

- `status`: `succeeded` / `failed` / `max_iterations`
- `output`: Parsed output (if `parse_final_output=True`)
- `raw_output`: Raw LLM text
- `iterations`: Actual iteration count
- `messages`: Full message history
- `tool_results`: All tool execution results
- `events`: All Keel events

## Context Management

`PrefixStableContext` assembles messages before each LLM call, preventing unbounded history growth:

```python
from keel_runtime import PrefixStableContext

context = PrefixStableContext(
    max_tokens=64_000,            # Token budget
    keep_recent_turns=10,         # Keep last N turns
    clear_consumed_results=True,  # Clear consumed tool results first
    cache_control=True,           # Output cache breakpoint metadata
)
```

Four-partition strategy:

1. **SYSTEM**: System prompt, never trimmed.
2. **TASK**: Initial task intent (first user + first assistant), never trimmed.
3. **HISTORY**: Intermediate history, trimmed first under token pressure.
4. **ACTIVE**: Recent active turns, preserved as much as possible.

Trimming order: clear consumed tool results first, then trim from the head of HISTORY. SYSTEM and TASK partitions stay stable.

You can replace the entire ContextProvider:

```python
from keel_runtime import ContextProvider, ContextResult

class MyContextProvider:
    async def build_messages(self, system_prompt, history, new_messages, config=None):
        # Your assembly logic
        return ContextResult(messages=..., tokens_used=..., cache_breakpoints=[])
```

## Tool Protocol

Define tools with the `@tool` decorator. Parameter schemas are auto-generated:

```python
from keel_runtime import tool, ToolRegistry

@tool(name="search_web", description="Search the web for information")
async def search_web(query: str, max_results: int = 5) -> list[dict]:
    # Your search logic
    return [{"title": "...", "url": "..."}]

@tool(
    name="send_email",
    side_effect=True,
    idempotency_key="email-notification-v1",
    timeout_seconds=10,
)
async def send_email(to: str, body: str) -> str:
    return "sent"

@tool(name="read_file", description="Read a file from workspace")
def read_file(path: str) -> str:
    return open(path).read()

# Register
registry = ToolRegistry([search_web, read_file])

# View generated schemas
print(registry.to_list())
# [{"name": "search_web", "description": "...", "parameters": {...}}, ...]

# Execute
from keel_runtime import ToolCall
result = await registry.execute(ToolCall(name="search_web", arguments={"query": "keel agent"}))
print(result.ok, result.output)
```

## Structured Output

Extract structured data from LLM text responses:

```python
from keel_runtime import parse_output, extract_json

# Auto-extract JSON (handles markdown code blocks)
text = 'Here is the result:\n```json\n{"score": 8.5, "summary": "Good"}\n```'
data = parse_output(text)
# {"score": 8.5, "summary": "Good"}

# Validate with Pydantic
from pydantic import BaseModel

class Review(BaseModel):
    score: float
    summary: str

review = parse_output(text, model=Review)
# Review(score=8.5, summary='Good')

# Strict mode: missing JSON or validation failure raises OutputValidationError
review = parse_output(text, model=Review, strict=True)

# Extract JSON only, no validation
raw = extract_json(text)
```

## Advanced Runtimes

`@agent` plus `JobManager.create_agent_job(...)` is the main embedded-agent path. Use `AgentSpec` and runtime adapters when you need external processes, containers, Kubernetes, or declarative workers.

### InProcessRuntime

Wrap a Python async callable as a Keel job, no subprocess required:

```python
from keel_runtime import JobManager, InProcessRuntime, AgentSpec

manager = JobManager(runtime=InProcessRuntime(), root=".keel")

async def my_handler(payload):
    return {"result": payload["task"] + " done"}

spec = AgentSpec(name="worker", command=my_handler)
job_id = manager.create_job(spec, input={"task": "process data"})

async for event in manager.stream(job_id):
    print(event.message)
```

## Job Lifecycle

```python
from keel_runtime import JobManager

manager = JobManager(root=".keel")

# domain_agent is the @agent wrapper from your domain module.
job_id = manager.create_agent_job(domain_agent, "Summarize this repo.")

# Stream output
async for event in manager.stream(job_id):
    print(event.message)

# Query status
status = manager.get_status(job_id)

# List artifacts
artifacts = manager.list_artifacts(job_id)

# Stop
await manager.stop(job_id)

# Explicit lifecycle APIs
async for event in manager.resume_restorable(job_id):
    ...

retry_id = manager.retry_failed(job_id, clean_workspace=False)

async for event in manager.replay(job_id):
    ...

# Restore a snapshot from object storage; this is not local continuation
manager.restore_job_from_storage(job_id)

# Export full record
manager.export_job(job_id)
```

## Model Configuration

```python
from keel_runtime import AgentSpec, ModelConfig

spec = AgentSpec(
    name="writer",
    model=ModelConfig(
        provider="openai",
        model="gpt-4.1",
        api_key_ref="OPENAI_API_KEY",
        fallback=ModelConfig(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            api_key_ref="ANTHROPIC_API_KEY",
        ),
    ),
    secret_env={"OPENAI_API_KEY": "sk-..."},
)
```

## Task Dependencies

```python
from keel_runtime import AgentSpec, ArtifactInput, JobManager

manager = JobManager(root=".keel")

first = manager.create_task(
    spec=AgentSpec(name="researcher"),
    input={"task": "analyze"},
)

second = manager.create_task(
    spec=AgentSpec(name="writer"),
    input={"task": "report"},
    dependencies=[first],
    artifact_inputs=[
        ArtifactInput(
            source_job_id=first,
            source_path="result.txt",
            target_path="inputs/research.txt",
        )
    ],
)
```

## Multi-Agent Collaboration

```python
from keel_runtime import AgentSpec, ArtifactInput, JobManager

manager = JobManager(root=".keel")
collab_id = manager.create_collaboration(
    goal="Review and improve code",
    workspace=".",
)

step1 = manager.add_collaboration_step(
    collab_id, AgentSpec(name="analyst"), {"task": "analyze"}
)
job1 = manager.get_collaboration_step(collab_id, step1).job_id

step2 = manager.add_collaboration_step(
    collab_id,
    AgentSpec(name="editor"),
    {"task": "apply fixes"},
    dependencies=[job1],
    artifact_inputs=[
        ArtifactInput(source_job_id=job1, source_path="result.txt", target_path="input.txt")
    ],
)

# Human confirmation
step3 = manager.add_collaboration_step(
    collab_id,
    AgentSpec(name="reviewer"),
    {"task": "final review"},
    requires_confirmation=True,
)
manager.confirm_collaboration_step(collab_id, step3, note="approved")
```

## Directory Structure

```text
keel/
  pyproject.toml
  src/
    keel_runtime/
      __init__.py
      loop.py            Agent loop
      context.py         Context management
      tools.py           Tool protocol
      output.py          Structured output
      runtime.py         Runtime adapters (InProcess / PiRpc / Docker / K8s)
      manager.py         JobManager
      specs.py           AgentSpec / ResourceLimits
      jobs.py            AgentJob / JobStatus
      models.py          ModelConfig / ModelUsage
      collaboration.py   Multi-agent collaboration
      stores.py          Persistent storage
      events.py          Streaming events
      security.py        Secret masking
      cleanup.py         Cleanup policies
      object_storage.py  S3/MinIO adapter
      errors.py          Exception hierarchy
  examples/
    fastapi_app/         FastAPI example service
  tests/
  design/                Logo and visual assets
```

## Installation

Python 3.11+:

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev,fastapi]"
```

S3/MinIO persistence:

```bash
pip install -e ".[s3]"
```

## Development & Testing

```bash
make check
```

## Status

**Recommended direction phases 1-6 are implemented in this branch.**

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Contracts and serialization | done |
| 2 | Checkpoint-aware AgentLoop | done |
| 3 | AgentLoopRuntime and persisted agent jobs | done |
| 4 | Explicit resume, retry, replay, and restore APIs | done |
| 5 | Tool contract and strict output | done |
| 6 | Public API, docs, and verifier cleanup | done |

## Design Principles

- **Toolkit, not a shell.** pip install, add a few lines, your code gains capabilities.
- **Generic mechanisms, no domain patterns.** Agent loop, context, tool protocol are generic; how you define agents, wire pipelines, that's your call.
- **Provider protocol, no locked implementation.** ContextProvider, LLM client are all replaceable.
- **Aligned with OpenAI Agents SDK patterns, not locked to OpenAI.** The LLM call layer is provider-agnostic. Add an adapter for Anthropic, Google, or any provider.
- **Zero external dependencies.** The core package has no third-party requirements.

## License

MIT
