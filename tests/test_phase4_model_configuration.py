from __future__ import annotations

import asyncio
import json
import sys
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

from keel_runtime import (
    AgentSpec,
    DockerRuntime,
    JobEvent,
    JobManager,
    JobStatus,
    KubernetesPodRuntime,
    ModelConfig,
    ModelUsage,
)
from keel_runtime.models import MODEL_USAGE_PREFIX
from keel_runtime.runtime import PiRpcRuntime


def collect(async_iter):
    async def _collect():
        return [event async for event in async_iter]

    return asyncio.run(_collect())


class UsageRuntime:
    def __init__(self, *, write_file: bool = False, bad_file: bool = False) -> None:
        self.write_file = write_file
        self.bad_file = bad_file

    async def run(self, job, spec) -> AsyncIterator[JobEvent]:
        usage = ModelUsage(
            provider="openai",
            model="gpt-4.1",
            input_tokens=11,
            output_tokens=7,
            total_tokens=18,
            cost_usd=0.002,
        ).to_dict()
        if self.bad_file:
            Path(job.artifact_path, "model-usage.json").write_text("{bad", encoding="utf-8")
        elif self.write_file:
            Path(job.artifact_path, "model-usage.json").write_text(
                json.dumps(usage),
                encoding="utf-8",
            )
        else:
            yield JobEvent.output(job.id, MODEL_USAGE_PREFIX + json.dumps(usage))
        yield JobEvent.output(job.id, "agent result")

    async def stop(self, job_id: str) -> None:
        return None


def test_legacy_model_dict_payload_is_unchanged(tmp_path: Path) -> None:
    runner = tmp_path / "payload_agent.py"
    runner.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "payload = json.load(sys.stdin)",
                "print(json.dumps(payload['agent']['model'], sort_keys=True), flush=True)",
            ]
        ),
        encoding="utf-8",
    )
    manager = JobManager(
        root=tmp_path / "data",
        runtime=PiRpcRuntime([sys.executable, str(runner)]),
    )
    legacy_model = {"model": "legacy-model-name", "temperature": 0.7}
    job_id = manager.create_job(AgentSpec(name="legacy", model=legacy_model), {"message": "ok"})

    events = collect(manager.stream(job_id))

    assert manager.get_status(job_id) == JobStatus.SUCCEEDED
    assert json.loads(events[1].message) == legacy_model


def test_model_config_payload_is_structured(tmp_path: Path) -> None:
    runner = tmp_path / "payload_agent.py"
    runner.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "payload = json.load(sys.stdin)",
                "print(json.dumps(payload['agent']['model'], sort_keys=True), flush=True)",
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
            name="structured",
            model=ModelConfig(
                provider="openai",
                model="gpt-4.1",
                api_key_ref="OPENAI_API_KEY",
                fallback=ModelConfig(provider="anthropic", model="claude-sonnet-4-20250514"),
            ),
            secret_env={"OPENAI_API_KEY": "secret-value"},
        ),
        {"message": "ok"},
    )

    events = collect(manager.stream(job_id))
    payload_model = json.loads(events[1].message)

    assert payload_model["provider"] == "openai"
    assert payload_model["api_key_ref"] == "OPENAI_API_KEY"
    assert payload_model["fallback"]["provider"] == "anthropic"
    assert "secret-value" not in json.dumps(manager.snapshot_job(job_id), ensure_ascii=False)


def test_model_config_warnings_are_recorded(tmp_path: Path) -> None:
    manager = JobManager(root=tmp_path, runtime=UsageRuntime())
    job_id = manager.create_job(
        AgentSpec(
            name="warnings",
            model=ModelConfig(
                provider="openai",
                model="not-in-default-list",
                api_key_ref="MISSING_KEY",
            ),
        ),
        {"message": "ok"},
    )

    events = manager.read_session(job_id)

    assert any(event.message == "model config warnings" for event in events)
    warning_event = next(event for event in events if event.message == "model config warnings")
    assert "api_key_ref is not available: MISSING_KEY" in warning_event.data["warnings"]


def test_model_usage_from_output_is_recorded_in_summary_snapshot_and_export(
    tmp_path: Path,
) -> None:
    manager = JobManager(root=tmp_path, runtime=UsageRuntime())
    job_id = manager.create_job(
        AgentSpec(
            name="usage",
            model=ModelConfig(provider="openai", model="gpt-4.1"),
        ),
        {"message": "ok"},
    )

    collect(manager.stream(job_id))

    summary = manager.describe_job(job_id)
    snapshot = manager.snapshot_job(job_id)
    export_path = manager.export_job(job_id)

    assert summary["job"]["model_usage"]["total_tokens"] == 18
    assert snapshot["job"]["model_usage"]["cost_usd"] == 0.002
    assert any(event.message == "model usage recorded" for event in manager.read_session(job_id))
    with zipfile.ZipFile(export_path) as archive:
        record = json.loads(archive.read("record.json"))
    assert record["job"]["model_usage"]["model"] == "gpt-4.1"


def test_model_usage_file_takes_precedence_and_bad_usage_only_warns(tmp_path: Path) -> None:
    manager = JobManager(root=tmp_path / "good", runtime=UsageRuntime(write_file=True))
    job_id = manager.create_job(
        AgentSpec(name="usage-file", model=ModelConfig(provider="openai", model="gpt-4.1")),
        {"message": "ok"},
    )
    collect(manager.stream(job_id))
    assert manager.get_job(job_id).model_usage is not None
    assert manager.get_job(job_id).model_usage.total_tokens == 18

    bad_manager = JobManager(root=tmp_path / "bad", runtime=UsageRuntime(bad_file=True))
    bad_id = bad_manager.create_job(
        AgentSpec(name="bad-usage", model=ModelConfig(provider="openai", model="gpt-4.1")),
        {"message": "ok"},
    )
    collect(bad_manager.stream(bad_id))

    assert bad_manager.get_status(bad_id) == JobStatus.SUCCEEDED
    assert bad_manager.get_job(bad_id).model_usage is None
    assert any(event.message == "model usage warning" for event in bad_manager.read_session(bad_id))


def test_model_api_key_ref_is_passed_without_secret_to_docker_and_kubernetes(
    tmp_path: Path,
) -> None:
    manager = JobManager(root=tmp_path / "data", runtime=UsageRuntime())
    job_id = manager.create_job(
        AgentSpec(
            name="runtime-secrets",
            model=ModelConfig(provider="openai", model="gpt-4.1", api_key_ref="OPENAI_API_KEY"),
            secret_env={"OPENAI_API_KEY": "secret-value"},
        ),
        {"message": "ok"},
    )
    job = manager.get_job(job_id)
    docker = DockerRuntime(image="keel-agent:latest")
    docker_args = docker.build_run_command(job, job.spec)
    manifest = KubernetesPodRuntime(
        image="keel-agent:latest",
        pvc_name="keel-pvc",
        secret_name="agent-secrets",
    ).build_pod_manifest(job, job.spec)
    manifest_text = json.dumps(manifest)

    assert "secret-value" not in " ".join(docker_args)
    assert "secret-value" not in manifest_text
    assert "--env OPENAI_API_KEY" in " ".join(docker_args)
    env_by_name = {entry["name"]: entry for entry in manifest["spec"]["containers"][0]["env"]}
    assert env_by_name["OPENAI_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "agent-secrets",
        "key": "OPENAI_API_KEY",
    }
