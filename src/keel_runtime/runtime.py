"""Agent runtime adapters."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Protocol

from keel_runtime.errors import RuntimeExecutionError
from keel_runtime.events import JobEvent
from keel_runtime.jobs import AgentJob
from keel_runtime.specs import AgentSpec


class AgentRuntime(Protocol):
    def run(self, job: AgentJob, spec: AgentSpec) -> AsyncIterator[JobEvent]:
        """Run a job and yield events."""

    async def stop(self, job_id: str) -> None:
        """Stop a running job."""


class PiRpcRuntime:
    """Run an agent through a local pi RPC-compatible command.

    The command receives a JSON payload on stdin, runs with the job workspace as
    its cwd, and streams stdout/stderr back as Keel events.
    """

    def __init__(self, command: Sequence[str] | None = None) -> None:
        self.command = list(command or ["pi", "rpc", "run"])
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._stopping: set[str] = set()

    async def run(self, job: AgentJob, spec: AgentSpec) -> AsyncIterator[JobEvent]:
        command = list(spec.command or self.command)
        if not command:
            raise RuntimeExecutionError("pi RPC command cannot be empty")

        env = os.environ.copy()
        env.update(spec.env)
        env.update(
            {
                "KEEL_JOB_ID": job.id,
                "KEEL_WORKSPACE_PATH": job.workspace_path,
                "KEEL_SESSION_PATH": job.session_path,
                "KEEL_ARTIFACT_PATH": job.artifact_path,
            }
        )
        payload = {
            "job_id": job.id,
            "input": job.input,
            "agent": spec.to_dict(),
            "workspace_path": job.workspace_path,
            "session_path": job.session_path,
            "artifact_path": job.artifact_path,
        }

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=job.workspace_path,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeExecutionError(f"pi RPC command not found: {command[0]}") from exc

        self._processes[job.id] = process
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        process.stdin.write_eof()

        queue: asyncio.Queue[JobEvent | None] = asyncio.Queue()

        async def pump(reader: asyncio.StreamReader | None, stderr: bool) -> None:
            if reader is None:
                await queue.put(None)
                return
            while line := await reader.readline():
                message = line.decode("utf-8", errors="replace").rstrip("\r\n")
                event = (
                    JobEvent.log(job.id, message, stream="stderr")
                    if stderr
                    else JobEvent.output(job.id, message, stream="stdout")
                )
                await queue.put(event)
            await queue.put(None)

        stdout_task = asyncio.create_task(pump(process.stdout, stderr=False))
        stderr_task = asyncio.create_task(pump(process.stderr, stderr=True))

        done_streams = 0
        while done_streams < 2:
            event = await queue.get()
            if event is None:
                done_streams += 1
                continue
            yield event

        await asyncio.gather(stdout_task, stderr_task)
        return_code = await process.wait()
        self._processes.pop(job.id, None)
        stopping = job.id in self._stopping
        self._stopping.discard(job.id)
        if return_code != 0 and not stopping:
            raise RuntimeExecutionError(f"pi RPC command exited with code {return_code}")

    async def stop(self, job_id: str) -> None:
        process = self._processes.get(job_id)
        if process is None:
            return
        self._stopping.add(job_id)
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


def resolve_store_path(path: str | Path | None = None) -> Path:
    return Path(path or ".keel").resolve()
