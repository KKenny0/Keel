"""Agent runtime adapters."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from keel_runtime.errors import RuntimeExecutionError, RuntimeTimeoutError
from keel_runtime.events import JobEvent
from keel_runtime.jobs import AgentJob
from keel_runtime.security import is_sensitive_key, redact_text
from keel_runtime.specs import AgentSpec


class AgentRuntime(Protocol):
    def run(self, job: AgentJob, spec: AgentSpec) -> AsyncIterator[JobEvent]:
        """Run a job and yield events."""

    async def stop(self, job_id: str) -> None:
        """Stop a running job."""


InProcessHandler = Callable[[dict[str, Any]], Awaitable[Any] | Any]


def _paths_payload(
    job: AgentJob,
    spec: AgentSpec,
    *,
    workspace_path: str | None = None,
    session_path: str | None = None,
    artifact_path: str | None = None,
) -> dict[str, Any]:
    workspace = workspace_path or job.workspace_path
    session = session_path or job.session_path
    artifact = artifact_path or job.artifact_path
    return {
        "job_id": job.id,
        "input": job.input,
        "agent": spec.to_dict(),
        "workspace_path": workspace,
        "session_path": session,
        "artifact_path": artifact,
    }


def _runtime_env(
    job: AgentJob,
    spec: AgentSpec,
    *,
    workspace_path: str | None = None,
    session_path: str | None = None,
    artifact_path: str | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(spec.runtime_env())
    env.update(
        {
            "KEEL_JOB_ID": job.id,
            "KEEL_WORKSPACE_PATH": workspace_path or job.workspace_path,
            "KEEL_SESSION_PATH": session_path or job.session_path,
            "KEEL_ARTIFACT_PATH": artifact_path or job.artifact_path,
        }
    )
    return env


def _format_inprocess_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, bytes):
        return result.decode("utf-8", errors="replace")
    try:
        return json.dumps(result, ensure_ascii=False)
    except TypeError:
        return str(result)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()


async def _stream_process(
    *,
    job_id: str,
    process: asyncio.subprocess.Process,
    timeout_seconds: float | None,
    secret_values: list[str],
    is_stopping: Callable[[], bool],
    command_label: str,
    stdout_as_log: bool = False,
) -> AsyncIterator[JobEvent]:
    queue: asyncio.Queue[JobEvent | None] = asyncio.Queue()

    async def pump(reader: asyncio.StreamReader | None, stderr: bool) -> None:
        if reader is None:
            await queue.put(None)
            return
        while line := await reader.readline():
            message = line.decode("utf-8", errors="replace").rstrip("\r\n")
            message = redact_text(message, secret_values)
            event = (
                JobEvent.log(job_id, message, stream="stderr")
                if stderr or stdout_as_log
                else JobEvent.output(job_id, message, stream="stdout")
            )
            await queue.put(event)
        await queue.put(None)

    stdout_task = asyncio.create_task(pump(process.stdout, stderr=False))
    stderr_task = asyncio.create_task(pump(process.stderr, stderr=True))
    deadline = (
        asyncio.get_running_loop().time() + timeout_seconds
        if timeout_seconds is not None
        else None
    )

    try:
        done_streams = 0
        while done_streams < 2:
            timeout = None
            if deadline is not None:
                timeout = max(0, deadline - asyncio.get_running_loop().time())
            try:
                event = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError as exc:
                await _stop_process(process)
                message = f"{command_label} timed out after {timeout_seconds:g} seconds"
                raise RuntimeTimeoutError(message) from exc
            if event is None:
                done_streams += 1
                continue
            yield event

        await asyncio.gather(stdout_task, stderr_task)
        return_code = await process.wait()
        if return_code != 0 and not is_stopping():
            raise RuntimeExecutionError(
                f"{command_label} exited with code {return_code}",
                exit_code=return_code,
            )
    finally:
        if not stdout_task.done():
            stdout_task.cancel()
        if not stderr_task.done():
            stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)


class InProcessRuntime:
    """Run a registered Python callable in the current event loop."""

    def __init__(
        self,
        handlers: Mapping[str, InProcessHandler] | None = None,
        *,
        default: InProcessHandler | None = None,
    ) -> None:
        self._handlers = dict(handlers or {})
        self._default = default
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._stopping: set[str] = set()

    def register(self, name: str, handler: InProcessHandler) -> None:
        if not name.strip():
            raise ValueError("in-process handler name cannot be empty")
        self._handlers[name] = handler

    async def run(self, job: AgentJob, spec: AgentSpec) -> AsyncIterator[JobEvent]:
        handler = self._resolve_handler(spec)
        payload = _paths_payload(job, spec)

        async def invoke() -> Any:
            result = handler(payload)
            if inspect.isawaitable(result):
                return await result
            return result

        task = asyncio.create_task(invoke(), name=f"keel-inprocess-{job.id}")
        self._tasks[job.id] = task
        yield JobEvent.status(job.id, "in-process callable started", agent_name=spec.name)

        try:
            try:
                result = await (
                    task
                    if spec.timeout_seconds is None
                    else asyncio.wait_for(task, timeout=spec.timeout_seconds)
                )
            except TimeoutError as exc:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                message = f"in-process callable timed out after {spec.timeout_seconds:g} seconds"
                raise RuntimeTimeoutError(message) from exc
            except asyncio.CancelledError:
                if job.id in self._stopping:
                    yield JobEvent.status(
                        job.id,
                        "in-process callable stopped",
                        agent_name=spec.name,
                    )
                    return
                raise

            if result is not None:
                yield JobEvent.output(
                    job.id,
                    _format_inprocess_result(result),
                    stream="inprocess",
                )
            yield JobEvent.status(job.id, "in-process callable completed", agent_name=spec.name)
        finally:
            self._tasks.pop(job.id, None)
            self._stopping.discard(job.id)

    async def stop(self, job_id: str) -> None:
        task = self._tasks.get(job_id)
        if task is None:
            return
        self._stopping.add(job_id)
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.CancelledError, TimeoutError):
            pass

    def _resolve_handler(self, spec: AgentSpec) -> InProcessHandler:
        handler = self._handlers.get(spec.name) or self._default
        if handler is None:
            raise RuntimeExecutionError(
                f"in-process handler not registered for agent: {spec.name}"
            )
        return handler


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

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=job.workspace_path,
                env=_runtime_env(job, spec),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeExecutionError(f"pi RPC command not found: {command[0]}") from exc

        self._processes[job.id] = process
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(_paths_payload(job, spec), ensure_ascii=False).encode("utf-8")
        )
        process.stdin.write_eof()

        try:
            async for event in _stream_process(
                job_id=job.id,
                process=process,
                timeout_seconds=spec.timeout_seconds,
                secret_values=spec.secret_values(),
                is_stopping=lambda: job.id in self._stopping,
                command_label="pi RPC command",
            ):
                yield event
        finally:
            self._processes.pop(job.id, None)
            self._stopping.discard(job.id)

    async def stop(self, job_id: str) -> None:
        process = self._processes.get(job_id)
        if process is None:
            return
        self._stopping.add(job_id)
        await _stop_process(process)


class DockerRuntime:
    """Run an agent command in a Docker container."""

    def __init__(
        self,
        image: str,
        *,
        command: Sequence[str] | None = None,
        docker_command: Sequence[str] | None = None,
        network: str | None = None,
        workspace_mount: str = "/workspace",
        session_mount: str = "/session",
        artifact_mount: str = "/artifacts",
        remove: bool = True,
    ) -> None:
        if not image.strip():
            raise ValueError("Docker image cannot be empty")
        self.image = image
        self.command = list(command or ["pi", "rpc", "run"])
        self.docker_command = list(docker_command or ["docker"])
        self.network = network
        self.workspace_mount = workspace_mount
        self.session_mount = session_mount
        self.artifact_mount = artifact_mount
        self.remove = remove
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._containers: dict[str, str] = {}
        self._stopping: set[str] = set()

    def build_run_command(self, job: AgentJob, spec: AgentSpec) -> list[str]:
        container_name = self._container_name(job.id)
        command = [*self.docker_command, "run"]
        if self.remove:
            command.append("--rm")
        command.extend(["--name", container_name, "-i"])
        if self.network:
            command.extend(["--network", self.network])
        command.extend(spec.resources.to_docker_args())
        command.extend(
            [
                "-v",
                f"{self._host_mount_path(job.workspace_path)}:{self.workspace_mount}",
                "-v",
                f"{self._host_mount_path(job.session_path)}:{self.session_mount}",
                "-v",
                f"{self._host_mount_path(job.artifact_path)}:{self.artifact_mount}",
                "--workdir",
                self.workspace_mount,
            ]
        )
        command.extend(self._docker_env_args(spec))
        command.append(self.image)
        command.extend(list(spec.command or self.command))
        return command

    async def run(self, job: AgentJob, spec: AgentSpec) -> AsyncIterator[JobEvent]:
        command = self.build_run_command(job, spec)
        container_name = self._container_name(job.id)
        env = _runtime_env(
            job,
            spec,
            workspace_path=self.workspace_mount,
            session_path=self.session_mount,
            artifact_path=self.artifact_mount,
        )

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
            raise RuntimeExecutionError(
                f"Docker command not found: {self.docker_command[0]}"
            ) from exc

        self._processes[job.id] = process
        self._containers[job.id] = container_name
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(
                _paths_payload(
                    job,
                    spec,
                    workspace_path=self.workspace_mount,
                    session_path=self.session_mount,
                    artifact_path=self.artifact_mount,
                ),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        process.stdin.write_eof()

        try:
            async for event in _stream_process(
                job_id=job.id,
                process=process,
                timeout_seconds=spec.timeout_seconds,
                secret_values=spec.secret_values(),
                is_stopping=lambda: job.id in self._stopping,
                command_label="Docker container",
            ):
                yield event
        finally:
            self._processes.pop(job.id, None)
            self._containers.pop(job.id, None)
            self._stopping.discard(job.id)

    async def stop(self, job_id: str) -> None:
        self._stopping.add(job_id)
        container_name = self._containers.get(job_id)
        if container_name:
            try:
                process = await asyncio.create_subprocess_exec(
                    *self.docker_command,
                    "stop",
                    container_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(process.wait(), timeout=5)
            except (FileNotFoundError, TimeoutError):
                pass
        process = self._processes.get(job_id)
        if process is not None:
            await _stop_process(process)

    def _docker_env_args(self, spec: AgentSpec) -> list[str]:
        args: list[str] = [
            "--env",
            "KEEL_JOB_ID",
            "--env",
            f"KEEL_WORKSPACE_PATH={self.workspace_mount}",
            "--env",
            f"KEEL_SESSION_PATH={self.session_mount}",
            "--env",
            f"KEEL_ARTIFACT_PATH={self.artifact_mount}",
        ]
        for key, value in spec.env.items():
            if is_sensitive_key(key):
                args.extend(["--env", str(key)])
            else:
                args.extend(["--env", f"{key}={value}"])
        for key in spec.secret_env:
            args.extend(["--env", str(key)])
        for key in spec.model_api_key_refs():
            if key not in spec.env and key not in spec.secret_env:
                args.extend(["--env", str(key)])
        return args

    @staticmethod
    def _container_name(job_id: str) -> str:
        return f"keel-{job_id[:32]}"

    @staticmethod
    def _host_mount_path(path: str) -> str:
        return str(Path(path).resolve()).replace("\\", "/")


class KubernetesPodRuntime:
    """Create a Kubernetes Pod backed by a PVC and stream its logs."""

    def __init__(
        self,
        image: str,
        pvc_name: str,
        *,
        command: Sequence[str] | None = None,
        kubectl_command: Sequence[str] | None = None,
        namespace: str | None = None,
        pvc_mount_path: str = "/keel",
        pod_name_prefix: str = "keel-job",
        secret_name: str = "keel-agent-secrets",
    ) -> None:
        if not image.strip():
            raise ValueError("Kubernetes image cannot be empty")
        if not pvc_name.strip():
            raise ValueError("Kubernetes PVC name cannot be empty")
        self.image = image
        self.pvc_name = pvc_name
        self.command = list(command or ["pi", "rpc", "run"])
        self.kubectl_command = list(kubectl_command or ["kubectl"])
        self.namespace = namespace
        self.pvc_mount_path = pvc_mount_path.rstrip("/")
        self.pod_name_prefix = pod_name_prefix
        self.secret_name = secret_name
        self._pods: dict[str, str] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._stopping: set[str] = set()

    def build_pod_manifest(self, job: AgentJob, spec: AgentSpec) -> dict[str, Any]:
        paths = self._container_paths(job.id)
        resources = spec.resources.to_kubernetes_resources()
        container: dict[str, Any] = {
            "name": "agent",
            "image": self.image,
            "workingDir": paths["workspace"],
            "command": [
                "/bin/sh",
                "-lc",
                f"cat {shlex.quote(paths['payload'])} | {shlex.join(spec.command or self.command)}",
            ],
            "env": self._kubernetes_env(spec, paths),
            "volumeMounts": [
                {
                    "name": "keel-pvc",
                    "mountPath": self.pvc_mount_path,
                }
            ],
        }
        if resources:
            container["resources"] = {"limits": resources}

        metadata: dict[str, Any] = {
            "name": self._pod_name(job.id),
            "labels": {"app": "keel-runtime", "keel-job-id": job.id},
        }
        if self.namespace:
            metadata["namespace"] = self.namespace

        spec_dict: dict[str, Any] = {
            "restartPolicy": "Never",
            "containers": [container],
            "volumes": [
                {
                    "name": "keel-pvc",
                    "persistentVolumeClaim": {"claimName": self.pvc_name},
                }
            ],
        }
        if spec.timeout_seconds is not None:
            spec_dict["activeDeadlineSeconds"] = self._kubectl_timeout(spec)

        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": metadata,
            "spec": spec_dict,
        }

    async def run(self, job: AgentJob, spec: AgentSpec) -> AsyncIterator[JobEvent]:
        pod_name = self._pod_name(job.id)
        self._pods[job.id] = pod_name
        runtime_dir = Path(job.session_path) / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = runtime_dir / "kubernetes-pod.json"
        payload_path = runtime_dir / "payload.json"
        paths = self._container_paths(job.id)
        payload_path.write_text(
            json.dumps(
                _paths_payload(
                    job,
                    spec,
                    workspace_path=paths["workspace"],
                    session_path=paths["session"],
                    artifact_path=paths["artifacts"],
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        manifest_path.write_text(
            json.dumps(self.build_pod_manifest(job, spec), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        yield JobEvent.status(
            job.id,
            "kubernetes pod manifest written",
            manifest_path=str(manifest_path),
        )

        try:
            async for event in self._run_kubectl(
                job,
                spec,
                ["apply", "-f", str(manifest_path)],
                stdout_as_log=True,
            ):
                yield event
            async for event in self._run_kubectl(job, spec, ["logs", "-f", pod_name]):
                yield event
            async for event in self._run_kubectl(
                job,
                spec,
                [
                    "wait",
                    f"pod/{pod_name}",
                    "--for=jsonpath={.status.phase}=Succeeded",
                    f"--timeout={self._kubectl_timeout(spec)}s",
                ],
                stdout_as_log=True,
            ):
                yield event
        finally:
            self._pods.pop(job.id, None)
            self._processes.pop(job.id, None)
            self._stopping.discard(job.id)

    async def stop(self, job_id: str) -> None:
        self._stopping.add(job_id)
        pod_name = self._pods.get(job_id)
        if pod_name:
            args = [*self.kubectl_command, *self._namespace_args(), "delete", "pod", pod_name]
            args.append("--ignore-not-found=true")
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(process.wait(), timeout=5)
            except (FileNotFoundError, TimeoutError):
                pass
        process = self._processes.get(job_id)
        if process is not None:
            await _stop_process(process)

    async def _run_kubectl(
        self,
        job: AgentJob,
        spec: AgentSpec,
        args: list[str],
        *,
        stdout_as_log: bool = False,
    ) -> AsyncIterator[JobEvent]:
        command = [*self.kubectl_command, *self._namespace_args(), *args]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=job.workspace_path,
                env=_runtime_env(job, spec),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeExecutionError(
                f"kubectl command not found: {self.kubectl_command[0]}"
            ) from exc

        self._processes[job.id] = process
        async for event in _stream_process(
            job_id=job.id,
            process=process,
            timeout_seconds=spec.timeout_seconds,
            secret_values=spec.secret_values(),
            is_stopping=lambda: job.id in self._stopping,
            command_label="kubectl command",
            stdout_as_log=stdout_as_log,
        ):
            yield event

    def _kubernetes_env(self, spec: AgentSpec, paths: dict[str, str]) -> list[dict[str, Any]]:
        entries = [
            {"name": "KEEL_JOB_ID", "value": paths["job_id"]},
            {"name": "KEEL_WORKSPACE_PATH", "value": paths["workspace"]},
            {"name": "KEEL_SESSION_PATH", "value": paths["session"]},
            {"name": "KEEL_ARTIFACT_PATH", "value": paths["artifacts"]},
        ]
        for key, value in spec.env.items():
            if is_sensitive_key(key):
                entries.append(self._secret_env_entry(key))
            else:
                entries.append({"name": str(key), "value": str(value)})
        for key in spec.secret_env:
            entries.append(self._secret_env_entry(key))
        for key in spec.model_api_key_refs():
            if key not in spec.env and key not in spec.secret_env:
                entries.append(self._secret_env_entry(key))
        return entries

    def _secret_env_entry(self, key: str) -> dict[str, Any]:
        return {
            "name": str(key),
            "valueFrom": {
                "secretKeyRef": {
                    "name": self.secret_name,
                    "key": str(key),
                }
            },
        }

    def _namespace_args(self) -> list[str]:
        return ["--namespace", self.namespace] if self.namespace else []

    def _container_paths(self, job_id: str) -> dict[str, str]:
        job_root = f"{self.pvc_mount_path}/jobs/{job_id}"
        return {
            "job_id": job_id,
            "workspace": f"{job_root}/workspace",
            "session": f"{job_root}/session",
            "artifacts": f"{job_root}/artifacts",
            "payload": f"{job_root}/session/runtime/payload.json",
        }

    def _pod_name(self, job_id: str) -> str:
        return f"{self.pod_name_prefix}-{job_id[:32]}"

    @staticmethod
    def _kubectl_timeout(spec: AgentSpec) -> int:
        if spec.timeout_seconds is None:
            return 86400
        return max(1, int(spec.timeout_seconds))


def resolve_store_path(path: str | Path | None = None) -> Path:
    return Path(path or ".keel").resolve()
