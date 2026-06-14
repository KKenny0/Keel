# Keel Agent Instructions

## Project Map

- `src/keel_runtime/agent.py`: public `@agent` decorator and `Agent` wrapper.
- `src/keel_runtime/loop.py`: agent loop, context assembly, tool execution, checkpoints, strict output.
- `src/keel_runtime/manager.py`: job lifecycle, persisted decorated-agent jobs, resume/retry/replay/restore APIs, collaborations.
- `src/keel_runtime/runtime.py`: runtime adapters, including `AgentLoopRuntime` and external process/container runtimes.
- `src/keel_runtime/stores.py`: local job layout, sessions, attempts, checkpoints, artifacts, object-storage sync.
- `examples/fastapi_app/app.py`: HTTP example for low-level job and collaboration APIs.
- `index.html` and `keel-landing.html`: duplicated landing-page entrypoints. Keep them content-identical unless intentionally splitting their roles.

## Verification

Run the stable project check before committing:

```bash
make check
```

If the system Python does not have dev dependencies installed, use a temporary virtualenv outside the repo and run:

```bash
PYTHONDONTWRITEBYTECODE=1 /path/to/venv/bin/python -m pytest -q -p no:cacheprovider
/path/to/venv/bin/python -m ruff check .
```

## Cleanup Rules

- Keep `.keel/`, `.od-skills/`, `*.artifact.json`, Python caches, pytest caches, ruff caches, and virtualenvs out of commits.
- Do not delete user or generated work just to make `git status` quieter; add or adjust ignore rules when the files are local generated state.
- Keep checkpoints and attempts under the job control directory, not under workspace or artifacts.

## API Boundaries

- Main embedded-agent path: `@agent` plus `JobManager.create_agent_job(...)`.
- Advanced runtime path: `AgentSpec` plus `JobManager.create_job(...)`.
- Use explicit lifecycle names in new docs and examples:
  `resume_restorable`, `retry_failed`, `replay`, and `restore_job_from_storage`.
- `restore_job` and `resume` are compatibility APIs only.
