# Keel Agent Runtime Recommended Direction Implementation

## 0. Document Meta

- Status: implementation plan
- Created: 2026-06-14
- Owner: Keel maintainers
- Scope: architecture refactor from isolated runtime primitives to one recoverable embedded agent runtime
- Source of truth: code under `src/keel_runtime/`, tests under `tests/`, and this document for the intended implementation sequence

This document turns the current architecture review into an implementation plan. It is not a marketing roadmap and not a broad rewrite request. The goal is to make the existing Keel primitives compose into the product promise:

> Keel is a Python toolkit embedded into a project. Domain authors write the system prompt, output type, and business tools; Keel provides the common runtime: agent loop, context management, tool execution, structured output, job persistence, and recovery.

## 1. Current Snapshot

Keel already has useful primitives:

- `Agent` / `@agent` wraps a Python callable and runs an `AgentLoop`.
- `AgentLoop` executes model calls, context assembly, tool calls, memory tools, gate tools, and output parsing.
- `PrefixStableContext` provides a stable-prefix context strategy.
- `ToolRegistry` and `@tool` provide schema generation and execution.
- `JobManager` persists job state, sessions, workspaces, artifacts, object-store snapshots, stop/resume, dependencies, and collaboration state.
- Runtime adapters exist for in-process, Pi RPC, Docker, and Kubernetes execution.

The current architecture has a product-level gap: `AgentLoop` and `JobManager` are parallel worlds.

```text
Current quick path

domain function
  |
  v
@agent -> Agent.__call__ -> AgentLoop.run
                         -> AgentLoopResult

No persisted checkpoint. No JobManager lifecycle.
```

```text
Current persisted path

AgentSpec + input
  |
  v
JobManager.create_job -> JobManager.stream
                      -> AgentRuntime.run(job, spec)
                      -> JobEvent / artifacts / job.json

No first-class AgentLoop execution plan.
```

Key evidence in current code:

- `Agent.__call__` calls `self.loop().run(...)` directly and returns `AgentLoopResult` (`src/keel_runtime/agent.py`, `Agent.__call__`).
- `AgentLoop.run` holds `events`, `context_results`, `tool_results`, `gate_decisions`, and active messages in memory until it returns (`src/keel_runtime/loop.py`, `AgentLoop.run`).
- `JobManager._run` delegates to `self.runtime.run(job, spec)` and persists emitted `JobEvent`s (`src/keel_runtime/manager.py`, `_run`).
- `JobManager.resume` resets non-succeeded jobs to `CREATED` and streams again; it does not continue from a loop checkpoint.
- `JobManager.restore_job` restores from configured object storage; it is snapshot restore, not local loop continuation.
- `parse_output` returns raw text when validation fails.
- `ToolSpec.execute` converts all exceptions to `ToolResult.failure(name, str(exc))`.

## 2. Problem Statement

The current implementation is locally clean but not yet architecturally elegant for the stated product target.

The main symptoms:

1. A user who wants persistence must leave the ergonomic `@agent` path and adopt `JobManager`, `AgentSpec`, runtime payload paths, and event streaming.
2. A user who uses `@agent` gets the agent loop, context, tools, memory, gates, and structured output, but not durable job lifecycle or recovery.
3. Recovery is ambiguous: `resume` means "run again with the same workspace", while `restore_job` means "restore a snapshot from object storage".
4. Tool execution and output parsing are not strict enough for reliable resume/retry decisions.
5. The top-level public API exposes too many advanced internals, making the intended main path hard to see.

## 3. Goals

The implementation must make one user path true:

```python
@keel.agent(
    client=client,
    system_prompt="...",
    output_type=MyOutput,
    tools=[search, write_report],
)
async def research_agent(topic: str) -> str:
    return topic

manager = keel.JobManager(root=".keel")
job_id = manager.create_agent_job(research_agent, "vector databases")

async for event in manager.stream(job_id):
    ...
```

Required capabilities:

- A decorated `Agent` can run directly or as a persisted `JobManager` job.
- `AgentLoop` writes recoverable checkpoints during execution.
- Restarted managers can mark interrupted loop jobs as restorable and continue from the last safe checkpoint.
- Failed jobs can be retried explicitly as a new attempt, with clear idempotency boundaries.
- Terminal jobs can be replayed as history without running again.
- Structured output can be strict, returning a typed result or a typed validation error.
- Tool errors have structured retry/safety metadata.
- Existing low-level runtime adapters remain available for advanced users.

## 4. Non-Goals

Do not implement these in this pass:

- A hosted Keel service.
- A scheduler, queue backend, or worker fleet.
- Cross-process distributed locking.
- Semantic memory or vector search.
- Provider-specific native structured-output adapters for every model vendor.
- Full sandbox/permission system for tools beyond a minimal execution contract.
- Breaking all existing imports immediately.

## 5. Target Architecture

The target architecture has one orchestration spine:

```text
                +----------------+
domain callable | @keel.agent    |
                +-------+--------+
                        |
                        v
                +----------------+
                | Agent object   |
                +-------+--------+
                        |
        direct call     | persisted call
        --------------- | --------------------------
                        v
                +----------------+
                | AgentLoopPlan  |
                +-------+--------+
                        |
                        v
                +----------------+
                | JobManager     |
                +-------+--------+
                        |
                        v
                +----------------+
                | AgentLoopRuntime
                +-------+--------+
                        |
                        v
                +----------------+
                | AgentLoop      |
                +-------+--------+
                        |
                        v
             checkpoints / events / artifacts
```

Direct invocation remains:

```text
await research_agent("topic") -> AgentLoopResult
```

Persisted invocation becomes:

```text
job_id = manager.create_agent_job(research_agent, "topic")
async for event in manager.stream(job_id): ...
```

The low-level runtime path still exists:

```text
JobManager.create_job(AgentSpec(...), input) -> runtime adapter
```

But it is explicitly an advanced path, not the default embedded-agent path.

## 6. Core Design Decisions

### Decision 1: Add `AgentLoopRuntime`

Create a runtime adapter that executes a Keel `Agent` / `AgentLoop` inside `JobManager`.

Responsibilities:

- Convert `AgentJob.input` into normalized agent input.
- Reconstruct `AgentLoop` from an `AgentLoopPlan`.
- Persist loop checkpoints after every durable boundary.
- Emit normal `JobEvent`s so existing `JobManager.stream` clients continue to work.
- Load the last checkpoint for restorable jobs.

Initial file targets:

- `src/keel_runtime/agent.py`
- `src/keel_runtime/loop.py`
- `src/keel_runtime/runtime.py`
- `src/keel_runtime/manager.py`
- `src/keel_runtime/jobs.py`
- `src/keel_runtime/stores.py`
- `tests/test_agent_job_runtime.py`

Bridge rules:

- Persisted agent jobs are created from an `Agent` object, not from raw `AgentSpec`.
- `ChatClient`, executable tools, context provider, memory provider, and gate provider come from the `Agent` instance.
- `AgentSpec` remains the low-level external-runtime declaration. Its current `tools: dict[str, Any]` field is not executable `ToolSpec` data.
- `ModelConfig` remains configuration metadata until a provider adapter layer exists. Do not pretend `ModelConfig` can construct a `ChatClient` in this refactor.
- `timeout_seconds` can be copied from a future persisted-agent option into the job/runtime layer, but it must not silently replace tool-level timeouts.

This keeps the embedded-toolkit promise honest: the domain author injects the real model adapter and business tools, while Keel supplies durable loop execution.

### Decision 2: Separate Resume, Retry, Restore, Replay

Current `resume(job_id)` conflates at least two meanings. The new API must make intent explicit:

- `resume_restorable(job_id)`: continue from a checkpoint; only valid for restorable jobs with a loop checkpoint.
- `retry_failed(job_id, clean_workspace=False)`: create a new attempt for a failed/stopped/restorable job.
- `replay(job_id)`: stream stored session events for terminal jobs without executing.
- `restore_job_from_storage(job_id)`: restore a snapshot from object storage.

Compatibility:

- Keep `resume(job_id)` temporarily.
- Deprecate it with runtime warnings and docs.
- Make it call `resume_restorable` only when a checkpoint exists; otherwise require explicit `retry_failed`.

### Decision 3: Introduce `JobAttempt`

Retries and resumes need a durable model instead of implicit workspace reuse.

Minimum model:

```json
{
  "id": "attempt-uuid",
  "job_id": "job-uuid",
  "number": 1,
  "kind": "initial | retry | resume",
  "retry_of": null,
  "resume_of": null,
  "idempotency_key": "optional-user-key",
  "status": "created | running | succeeded | failed | stopped",
  "started_at": "2026-06-14T00:00:00Z",
  "ended_at": null,
  "error": null,
  "retryable": null
}
```

Storage:

```text
.keel/jobs/{job_id}/job.json
.keel/jobs/{job_id}/attempts/{attempt_id}.json
.keel/jobs/{job_id}/session/events.jsonl
.keel/jobs/{job_id}/checkpoints/agent-loop.json
.keel/jobs/{job_id}/workspace/
.keel/jobs/{job_id}/artifacts/
```

### Decision 4: Add Loop Checkpoints at Durable Boundaries

Checkpoint after these boundaries:

1. Agent loop started.
2. Context built for an iteration.
3. Model response received.
4. Assistant tool-call message appended.
5. Each tool result appended.
6. Gate decision recorded.
7. Final output parsed.
8. Terminal status selected.

Minimum checkpoint schema:

```json
{
  "version": 1,
  "job_id": "job-uuid",
  "attempt_id": "attempt-uuid",
  "agent_name": "research-agent",
  "iteration": 2,
  "status": "running | awaiting_tool | awaiting_gate | completed | failed",
  "history_messages": [],
  "active_messages": [],
  "pending_tool_calls": [],
  "completed_tool_results": [],
  "context_results": [],
  "composed_prompts": [],
  "gate_decisions": [],
  "raw_output": null,
  "parsed_output": null,
  "parse_error": null,
  "created_at": "2026-06-14T00:00:00Z",
  "updated_at": "2026-06-14T00:00:01Z"
}
```

The first implementation can store only JSON-serializable fields already exposed by `Message`, `ToolCall`, `ToolResult`, `ContextResult`, `GateDecision`, and `JobEvent`. If a field is not serializable, define a `to_dict` / `from_dict` pair before checkpointing it.

### Decision 5: Make Structured Output Strict by Choice

Current `parse_output` keeps the old convenience fallback. Add a strict mode and expose it through `@agent`.

Target API:

```python
@keel.agent(
    client=client,
    output_type=Report,
    output_mode="strict",
)
async def reporter(topic: str) -> str:
    return topic
```

Behavior:

- `output_mode="fallback"` keeps current behavior.
- `output_mode="strict"` returns a failed loop result or raises a typed `OutputValidationError`, depending on API layer.
- `AgentLoopResult` stores `output_error` when strict parsing fails.
- Job execution records strict output failure as a structured `JobEvent.error`.

Minimum error shape:

```json
{
  "code": "output_validation_failed",
  "message": "model validation failed",
  "raw_output": "...",
  "retryable": false
}
```

### Decision 6: Add a Minimal Tool Execution Contract

Do not build a full sandbox yet. Add enough metadata for resume/retry safety.

Target additions:

```python
@keel.tool(
    name="send_email",
    side_effect=True,
    idempotency_required=True,
    timeout_seconds=30,
)
async def send_email(to: str, subject: str, body: str, idempotency_key: str) -> str:
    ...
```

Minimum `ToolResult` additions:

```json
{
  "name": "send_email",
  "ok": false,
  "output": null,
  "error": {
    "code": "timeout",
    "message": "tool timed out after 30 seconds",
    "retryable": true,
    "safe_to_retry": false
  },
  "call_id": "call-1"
}
```

Rules:

- Unknown tool: retryable false.
- Validation error: retryable false.
- Timeout: retryable true, safe_to_retry depends on `side_effect`.
- Side-effect tool without idempotency key: fail before execution when the job is persisted.
- Direct non-persisted `@agent` calls may warn instead of failing in the first version.

Default tool-error behavior stays compatible:

- In fallback mode, non-fatal tool errors are still written as tool messages and can be fed back to the model.
- In persisted strict mode, tool errors include `code`, `retryable`, and `safe_to_retry` so `resume_restorable` and `retry_failed` can make deterministic decisions.
- `fail_on_tool_error=True` still terminates the loop, but it should now terminate with a structured error.

## 7. Public API Plan

### Main API

Keep these at `keel_runtime` top level:

- `agent`
- `Agent`
- `tool`
- `ToolResult`
- `ToolError`
- `AgentLoopResult`
- `JobManager`
- `LocalMemoryProvider`
- `MemoryProvider`
- `OutputValidationError`

Add:

- `AgentLoopRuntime`
- `JobAttempt`
- `AgentLoopCheckpoint`

But expose new runtime/checkpoint classes initially as public advanced objects only if tests or users need them directly.

### Advanced API

Move documentation emphasis, not code immediately:

- `DockerRuntime`
- `KubernetesPodRuntime`
- `PiRpcRuntime`
- `LocalStores`
- `ObjectStorage`
- `ProviderRegistry`
- raw `AgentSpec`

Do not remove imports in the first pass. Mark them as advanced in docs and start a deprecation policy only after a stable main path exists.

## 8. Implementation Phases

Each phase must be independently mergeable.

### Phase 1: Contracts and Serialization

Scope:

- Add serializable models for `AgentLoopCheckpoint`, `JobAttempt`, `ToolError`, and `OutputValidationError`.
- Add store read/write helpers for attempts and checkpoints.
- Add tests for round-trip serialization.

Files:

- `src/keel_runtime/jobs.py`
- `src/keel_runtime/stores.py`
- `src/keel_runtime/tools.py`
- `src/keel_runtime/output.py`
- `tests/test_agent_loop_checkpoint.py`
- `tests/test_job_attempts.py`
- `tests/test_tool_errors.py`
- `tests/test_strict_output.py`

Acceptance:

- Existing tests remain compatible.
- New models serialize to plain JSON.
- No behavior change to direct `@agent` calls yet.

Verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3.12 -m pytest -q \
  tests/test_agent_loop_checkpoint.py \
  tests/test_job_attempts.py \
  tests/test_tool_errors.py \
  tests/test_strict_output.py \
  -p no:cacheprovider
```

### Phase 2: Checkpoint-Aware AgentLoop

Scope:

- Add optional checkpoint sink/source to `AgentLoop.run`.
- Persist checkpoint snapshots after each durable boundary.
- Add resume-from-checkpoint support for in-memory tests.
- Keep direct call behavior unchanged when no checkpoint sink/source is supplied.

Files:

- `src/keel_runtime/loop.py`
- `src/keel_runtime/context.py` only if `ContextResult` serialization needs support
- `src/keel_runtime/gate.py` only if `GateDecision` serialization needs support
- `tests/test_phase55_agent_loop.py`
- `tests/test_agent_loop_checkpoint.py`

Acceptance:

- A loop can resume from a checkpoint after a completed tool result without re-running that tool.
- A loop can replay completed messages and continue to the next model call.
- Max-iteration and tool-error behavior remains compatible.

Verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3.12 -m pytest -q \
  tests/test_phase55_agent_loop.py \
  tests/test_agent_loop_checkpoint.py \
  -p no:cacheprovider
```

### Phase 3: AgentLoopRuntime and Persisted Agent Jobs

Scope:

- Add `AgentLoopRuntime`.
- Add `JobManager.create_agent_job(agent, *args, **kwargs)` or equivalent explicit API.
- Add `Agent.as_job_handler()` only if needed to keep runtime registration simple.
- Persist checkpoints under `.keel/jobs/{job_id}/checkpoints/agent-loop.json`.
- Stream loop events through `JobManager`.

Files:

- `src/keel_runtime/agent.py`
- `src/keel_runtime/runtime.py`
- `src/keel_runtime/manager.py`
- `src/keel_runtime/__init__.py`
- `tests/test_agent_job_runtime.py`
- `examples/quickstart_agent.py`

Acceptance:

- Existing quickstart still works by direct call.
- A decorated agent can be run as a job without manually writing `AgentSpec`.
- Persisted agent job writes normal session events, result artifact, and checkpoint.
- Restarting `JobManager` after a running agent job marks it restorable.
- `JobManager._run` does not blindly concatenate all agent-loop output events into a misleading `result.txt`; parsed final output, raw final output, tool events, and diagnostic events are preserved under distinct event/artifact names.

Verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3.12 -m pytest -q \
  tests/test_phase55_quickstart.py \
  tests/test_agent_job_runtime.py \
  tests/test_phase1_local_mvp.py \
  -p no:cacheprovider
```

### Phase 4: Explicit Resume, Retry, Replay, Restore APIs

Scope:

- Add `resume_restorable`, `retry_failed`, `replay`, and `restore_job_from_storage`.
- Keep `resume` as a compatibility wrapper with clear warnings.
- Add attempt records.
- Make collaboration retry use the same attempt model.

Files:

- `src/keel_runtime/manager.py`
- `src/keel_runtime/jobs.py`
- `src/keel_runtime/collaboration.py`
- `tests/test_phase2_persistence.py`
- `tests/test_phase5_multi_agent_collaboration.py`
- `tests/test_job_attempts.py`

Acceptance:

- `resume_restorable` does not re-run completed safe tool calls.
- `retry_failed` creates a new attempt and records `retry_of`.
- `replay` reads stored events only.
- `restore_job_from_storage` replaces `restore_job` in docs while compatibility remains.

Verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3.12 -m pytest -q \
  tests/test_phase2_persistence.py \
  tests/test_phase5_multi_agent_collaboration.py \
  tests/test_job_attempts.py \
  -p no:cacheprovider
```

### Phase 5: Tool Contract and Strict Output

Scope:

- Extend `@tool` / `ToolSpec` with timeout, side-effect, idempotency metadata, and typed errors.
- Add strict output mode to `parse_output`, `AgentLoopConfig`, and `@agent`.
- Preserve current fallback behavior by default for compatibility.

Files:

- `src/keel_runtime/tools.py`
- `src/keel_runtime/output.py`
- `src/keel_runtime/loop.py`
- `src/keel_runtime/agent.py`
- `tests/test_phase55_tools.py`
- `tests/test_phase55_output.py`
- `tests/test_strict_output.py`
- `tests/test_tool_errors.py`

Acceptance:

- Current tests still pass.
- Strict output test fails deterministically on invalid typed output.
- Side-effect tool without idempotency key fails before persisted execution.
- Timeout errors are structured.

Verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3.12 -m pytest -q \
  tests/test_phase55_tools.py \
  tests/test_phase55_output.py \
  tests/test_strict_output.py \
  tests/test_tool_errors.py \
  -p no:cacheprovider
```

### Phase 6: Public API, Docs, and Verifier Cleanup

Scope:

- Rewrite README quickstart around the single decorated-agent path.
- Add an "Advanced runtimes" section for `AgentSpec`, Pi RPC, Docker, Kubernetes, and object storage.
- Fix stale landing snippets (`export_artifacts`, `LocalStore`, `save_artifact`).
- Remove phase labels from user-facing runtime errors.
- Add a stable verification wrapper.
- Decide ownership for duplicated tracked HTML entrypoints.
- Align `examples/fastapi_app/app.py` with the explicit resume/retry/replay/restore API names.

Files:

- `README.md`
- `README.en.md`
- `index.html`
- `keel-landing.html`
- `examples/fastapi_app/app.py`
- `src/keel_runtime/context.py`
- `pyproject.toml` or `Makefile`
- `AGENTS.md`

Recommended verifier wrapper:

```makefile
.PHONY: check
check:
	python3.12 -m pytest -q
	python3.12 -m ruff check .
```

Acceptance:

- README no longer implies `restore_job` resumes local interrupted work.
- Landing page snippets use real API names.
- `AGENTS.md` documents project map, verification commands, and cleanup rules.
- `make check` or another stable wrapper exists.
- HTTP examples use mutating verbs for mutating operations. Resume/retry endpoints should be `POST`, not `GET`, if the FastAPI example continues to expose them.

Verification:

```bash
rg -n "export_artifacts|LocalStore|save_artifact|Phase 5.5|restore_job\\(" README*.md index.html keel-landing.html src
PYTHONDONTWRITEBYTECODE=1 python3.12 -m pytest -q -p no:cacheprovider
```

## 9. Data Migration and Compatibility

Existing `.keel/jobs/{job_id}/job.json` files should remain readable.

Migration rules:

- If a job has no `attempts/`, synthesize one read-only legacy attempt when loading.
- If a job has no checkpoint, `resume_restorable` must fail with a clear error and suggest `retry_failed`.
- `restore_job` should continue to work for one compatibility window, but docs should use `restore_job_from_storage`.
- Existing `AgentLoopResult` fields must remain available.
- Existing `parse_output(text, model)` default behavior remains fallback unless strict mode is enabled.
- Existing `ToolResult.error` string access should remain possible in the first migration pass; typed errors can expose `.message` and serialize to the old string when needed.

## 10. Verification Matrix

| Area | Happy Path | Failure Path | Edge Case |
| --- | --- | --- | --- |
| Direct `@agent` | quickstart returns final output | tool error feeds back to model | max iterations |
| Persisted agent job | decorated agent streams through `JobManager` | runtime crash marks restorable | restart after checkpoint |
| Resume | continues from checkpoint | missing checkpoint fails clearly | pending gate checkpoint |
| Retry | new attempt records `retry_of` | retry limit rejects | workspace clean vs reuse |
| Structured output | typed output validates | strict validation fails job | fallback mode returns text |
| Tools | schema + execution succeed | typed `ToolError` emitted | side-effect without idempotency |
| Object storage | snapshot uploads manifest | storage failure marks job failed | restore to new root |
| Docs/API | snippets match real API | stale names caught by `rg` | duplicated HTML ownership |

Full local validation should run in a Python 3.11+ environment. Current project metadata requires Python 3.11 or newer.

Preferred command after installing dev dependencies:

```bash
python3.12 -m pip install -e ".[dev,fastapi]"
PYTHONDONTWRITEBYTECODE=1 python3.12 -m pytest -q -p no:cacheprovider
python3.12 -m ruff check .
```

If the local Python 3.12 interpreter has no pytest installed and the repo should remain clean, use a temporary venv outside the repository:

```bash
/opt/homebrew/bin/python3.12 -m venv /private/tmp/keel-py312
/private/tmp/keel-py312/bin/pip install -e ".[dev,fastapi]"
PYTHONDONTWRITEBYTECODE=1 /private/tmp/keel-py312/bin/python -m pytest -q -p no:cacheprovider
```

## 11. Risks and Failure Modes

### Risk: Checkpoints accidentally re-run side effects

Mitigation:

- Do not resume across an in-flight side-effect tool unless a completed `ToolResult` exists.
- Persist pending tool calls separately from completed tool results.
- Require idempotency metadata for side-effect tools in persisted jobs.

### Risk: Compatibility wrapper keeps ambiguous `resume`

Mitigation:

- Emit a deprecation warning from `resume`.
- Update README and examples first.
- Add tests that require explicit `retry_failed` when no checkpoint exists.

### Risk: Public API cleanup becomes breaking

Mitigation:

- Do not remove top-level exports in this implementation.
- Reframe docs first.
- Add deprecation warnings only after the main persisted-agent path is stable.

### Risk: Checkpoint schema becomes too broad

Mitigation:

- Store only the fields needed to continue execution.
- Keep raw event history in `events.jsonl`, not duplicated inside every checkpoint.
- Version checkpoint schema from day one.

### Risk: Result artifacts duplicate or lose final output

Current `JobManager._run` aggregates every output event into one `result.txt`. Once `AgentLoopRuntime` emits tool, diagnostic, raw final, and parsed final events, that aggregation can create a misleading artifact.

Mitigation:

- Reserve `result.txt` for final user-facing raw output only.
- Store typed output separately as `output.json` when it is JSON serializable.
- Keep tool outputs in session events unless a tool explicitly writes artifacts.
- Add tests for jobs with multiple tool output events and one final answer.

### Risk: Cleanup policy removes state needed for resume or audit

Current cleanup can remove workspace/artifacts for terminal jobs. Checkpoints and attempts must remain audit-safe unless explicitly cleaned.

Mitigation:

- Never place loop checkpoints under workspace or artifacts.
- Keep attempts and checkpoints under the job control directory.
- Make cleanup policy explicitly decide whether checkpoints are removed.
- In docs, warn that removing checkpoints makes `resume_restorable` impossible.

### Risk: Strict output creates too much friction

Mitigation:

- Keep fallback mode as default for current users.
- Make strict mode explicit through `output_mode="strict"`.
- Document when strict mode is appropriate: production APIs, persisted jobs, and typed downstream contracts.

## 12. Rollback Strategy

Each phase is rollback-safe:

- Phase 1 adds models and tests; rollback removes unused types.
- Phase 2 adds optional checkpoint hooks; direct `AgentLoop.run` remains unchanged without a sink/source.
- Phase 3 adds a new persisted-agent path; existing `create_job(AgentSpec, input)` remains unchanged.
- Phase 4 adds explicit APIs while preserving `resume`.
- Phase 5 preserves fallback parsing and old tool behavior by default.
- Phase 6 is docs/verifier cleanup and can be reverted independently.

If a phase fails after release:

1. Disable the new API in docs.
2. Keep old direct `@agent` and `create_job` paths.
3. Leave checkpoint files ignored by older code.
4. Add a migration note before removing any persisted files.

## 13. Implementation Order

Recommended order:

1. Contracts and serialization.
2. Checkpoint-aware `AgentLoop`.
3. `AgentLoopRuntime` and `create_agent_job`.
4. Explicit resume/retry/replay/restore APIs.
5. Tool execution contract and strict output.
6. Public API/docs/verifier cleanup.

Do not start with docs polish. The first architectural value comes from proving one persisted decorated agent can checkpoint, stop, restart, and continue without re-running completed safe work.

## 14. Acceptance Criteria for the Whole Direction

The direction is complete when all of these are true:

- A new user can build a real agent with `@keel.agent`, tools, system prompt, and output type without touching `AgentSpec`.
- The same agent can be run under `JobManager` with durable events, artifacts, and checkpoints.
- A manager restart during an agent loop marks the job restorable.
- `resume_restorable` continues from the last safe checkpoint.
- `retry_failed` creates a new attempt instead of pretending to continue.
- `restore_job_from_storage` is clearly separate from local resume.
- Strict structured output can fail deterministically.
- Tool failures are structured enough for retry decisions.
- README, landing page snippets, examples, and tests all describe the same API.
- The repo has a stable verification command and a tracked agent instruction surface.

## 15. Immediate Follow-Up Checklist

Before implementation starts:

- Clean or intentionally track the current Open Design generated artifacts:
  `git clean -nd -- .od-skills index.html.artifact.json keel-landing.html.artifact.json`
- Install a Python 3.11+ dev environment with pytest:
  `python3.12 -m pip install -e ".[dev,fastapi]"`
- Add or approve `AGENTS.md` as the project instruction source of truth.
- Decide whether `index.html` and `keel-landing.html` are both intentional public entrypoints.
- Confirm that persisted side-effect tools must require idempotency metadata in v1.

## 16. Source of Truth

Code remains the source of truth during implementation. This document records the intended architectural direction and merge sequence. If code and this document disagree, update this document in the same change that changes the architecture.
