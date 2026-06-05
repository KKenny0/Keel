from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from keel_runtime import (
    AgentSpec,
    CleanupPolicy,
    DockerRuntime,
    JobEvent,
    JobManager,
    JobStatus,
    KubernetesPodRuntime,
    PiRpcRuntime,
    ResourceLimits,
)


def collect(async_iter):
    async def _collect():
        return [event async for event in async_iter]

    return asyncio.run(_collect())


class WorkspaceRuntime:
    async def run(self, job, spec) -> AsyncIterator[JobEvent]:
        workspace = Path(job.workspace_path)
        (workspace / "kept.txt").write_text("workspace data", encoding="utf-8")
        yield JobEvent.output(job.id, "cleanup-result")

    async def stop(self, job_id: str) -> None:
        return None


def test_local_runtime_timeout_marks_job_failed_with_reason(tmp_path: Path) -> None:
    runner = tmp_path / "sleepy_agent.py"
    runner.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "import time",
                "json.load(sys.stdin)",
                "time.sleep(5)",
                "print('too late', flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    manager = JobManager(
        root=tmp_path / "data",
        runtime=PiRpcRuntime([sys.executable, str(runner)]),
    )
    job_id = manager.create_job(
        AgentSpec(name="timeout", timeout_seconds=0.05),
        {"message": "slow"},
    )

    events = collect(manager.stream(job_id))

    job = manager.get_job(job_id)
    assert job.status == JobStatus.FAILED
    assert job.timed_out is True
    assert "timed out" in (job.error or "")
    assert any(event.type.value == "error" and event.data["timed_out"] for event in events)


def test_local_runtime_records_crash_exit_code(tmp_path: Path) -> None:
    runner = tmp_path / "crashing_agent.py"
    runner.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "json.load(sys.stdin)",
                "print('before crash', flush=True)",
                "sys.exit(7)",
            ]
        ),
        encoding="utf-8",
    )
    manager = JobManager(
        root=tmp_path / "data",
        runtime=PiRpcRuntime([sys.executable, str(runner)]),
    )
    job_id = manager.create_job(AgentSpec(name="crash"), {"message": "crash"})

    events = collect(manager.stream(job_id))

    job = manager.get_job(job_id)
    assert job.status == JobStatus.FAILED
    assert job.exit_code == 7
    assert job.timed_out is False
    assert "code 7" in (job.error or "")
    assert any(event.type.value == "error" and event.data["exit_code"] == 7 for event in events)


def test_sensitive_env_is_redacted_from_records_logs_and_artifacts(tmp_path: Path) -> None:
    runner = tmp_path / "secret_agent.py"
    runner.write_text(
        "\n".join(
            [
                "import json",
                "import os",
                "import sys",
                "json.load(sys.stdin)",
                "print('token=' + os.environ['API_TOKEN'], flush=True)",
                "print('hidden=' + os.environ['HIDDEN_TOKEN'], file=sys.stderr, flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    manager = JobManager(
        root=tmp_path / "data",
        runtime=PiRpcRuntime([sys.executable, str(runner)]),
    )
    job_id = manager.create_job(
        AgentSpec(
            name="secret",
            env={"API_TOKEN": "super-secret-token", "MODE": "prod"},
            secret_env={"HIDDEN_TOKEN": "hidden-value"},
        ),
        {"message": "secret"},
    )

    collect(manager.stream(job_id))

    job_json = json.dumps(manager.get_job(job_id).to_dict(), ensure_ascii=False)
    session_json = "\n".join(
        json.dumps(event.to_dict(), ensure_ascii=False) for event in manager.read_session(job_id)
    )
    result = manager.download_artifact(job_id, "result.txt").decode("utf-8")

    assert "super-secret-token" not in job_json
    assert "super-secret-token" not in session_json
    assert "super-secret-token" not in result
    assert "hidden-value" not in job_json
    assert "hidden-value" not in session_json
    assert "[redacted]" in session_json
    assert "[redacted]" in result
    assert '"MODE": "prod"' in job_json


def test_docker_runtime_runs_with_resource_limits_and_without_secret_args(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_docker = tmp_path / "fake_docker.py"
    docker_log = tmp_path / "docker-args.jsonl"
    fake_docker.write_text(
        "\n".join(
            [
                "import json",
                "import os",
                "import pathlib",
                "import sys",
                "pathlib.Path(os.environ['FAKE_DOCKER_LOG']).write_text(",
                "    json.dumps(sys.argv[1:]) + '\\n', encoding='utf-8'",
                ")",
                "payload = json.load(sys.stdin)",
                "print('docker:' + payload['input']['message'], flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(docker_log))
    manager = JobManager(
        root=tmp_path / "data",
        runtime=DockerRuntime(
            image="keel-agent:latest",
            command=["agent"],
            docker_command=[sys.executable, str(fake_docker)],
        ),
    )
    spec = AgentSpec(
        name="docker",
        env={"API_TOKEN": "docker-secret", "MODE": "prod"},
        resources=ResourceLimits(cpu="1", memory="512m"),
    )
    job_id = manager.create_job(spec, {"message": "ok"})

    events = collect(manager.stream(job_id))

    args = json.loads(docker_log.read_text(encoding="utf-8").strip())
    args_text = " ".join(args)
    assert manager.get_status(job_id) == JobStatus.SUCCEEDED
    assert any(event.message == "docker:ok" for event in events)
    assert "--cpus" in args
    assert "1" in args
    assert "--memory" in args
    assert "512m" in args
    assert f"keel-{job_id[:32]}" in args
    assert job_id in args_text
    assert "docker-secret" not in args_text
    assert "--env API_TOKEN" in args_text


def test_kubernetes_runtime_writes_pvc_manifest_and_streams_logs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_kubectl = tmp_path / "fake_kubectl.py"
    kubectl_log = tmp_path / "kubectl-args.jsonl"
    fake_kubectl.write_text(
        "\n".join(
            [
                "import json",
                "import os",
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "log_path = pathlib.Path(os.environ['FAKE_KUBECTL_LOG'])",
                "with log_path.open('a', encoding='utf-8') as handle:",
                "    handle.write(json.dumps(args) + '\\n')",
                "if 'logs' in args:",
                "    print('k8s:ok', flush=True)",
                "elif 'wait' in args:",
                "    print('pod/succeeded', flush=True)",
                "else:",
                "    print('pod/applied', flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_KUBECTL_LOG", str(kubectl_log))
    runtime = KubernetesPodRuntime(
        image="keel-agent:latest",
        pvc_name="keel-pvc",
        command=["agent"],
        kubectl_command=[sys.executable, str(fake_kubectl)],
        namespace="agents",
        secret_name="agent-secrets",
    )
    manager = JobManager(root=tmp_path / "data", runtime=runtime)
    spec = AgentSpec(
        name="k8s",
        env={"API_TOKEN": "k8s-secret", "MODE": "prod"},
        resources=ResourceLimits(cpu="500m", memory="512Mi", ephemeral_storage="1Gi"),
        timeout_seconds=5,
    )
    job_id = manager.create_job(spec, {"message": "k8s"})

    events = collect(manager.stream(job_id))

    manifest_path = Path(manager.get_job(job_id).session_path) / "runtime" / "kubernetes-pod.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    container = manifest["spec"]["containers"][0]
    env_by_name = {entry["name"]: entry for entry in container["env"]}

    assert manager.get_status(job_id) == JobStatus.SUCCEEDED
    assert any(event.message == "k8s:ok" for event in events)
    assert manifest["metadata"]["namespace"] == "agents"
    assert manifest["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "keel-pvc"
    assert container["volumeMounts"][0]["mountPath"] == "/keel"
    assert container["resources"]["limits"] == {
        "cpu": "500m",
        "memory": "512Mi",
        "ephemeral-storage": "1Gi",
    }
    assert env_by_name["API_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "agent-secrets",
        "key": "API_TOKEN",
    }
    assert env_by_name["MODE"]["value"] == "prod"
    assert f"/keel/jobs/{job_id}/workspace" in manifest_text
    assert "k8s-secret" not in manifest_text


def test_cleanup_policy_removes_workspace_after_success(tmp_path: Path) -> None:
    manager = JobManager(
        root=tmp_path,
        runtime=WorkspaceRuntime(),
        cleanup_policy=CleanupPolicy(remove_workspace_on_success=True),
    )
    job_id = manager.create_job(AgentSpec(name="cleanup"), {"message": "clean"})

    collect(manager.stream(job_id))

    job = manager.get_job(job_id)
    assert job.status == JobStatus.SUCCEEDED
    assert not Path(job.workspace_path).exists()
    assert manager.download_artifact(job_id, "result.txt") == b"cleanup-result"
    assert any(event.message == "job cleaned" for event in manager.read_session(job_id))
