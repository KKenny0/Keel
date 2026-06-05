"""Local filesystem stores for jobs, sessions, workspaces, and artifacts."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from keel_runtime.errors import JobNotFoundError
from keel_runtime.events import JobEvent
from keel_runtime.jobs import AgentJob


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class JobLayout:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.jobs_root = self.root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    def job_file(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    def session_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "session"

    def session_file(self, job_id: str) -> Path:
        return self.session_dir(job_id) / "events.jsonl"

    def workspace_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "workspace"

    def artifact_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "artifacts"

    def ensure_job_dirs(self, job_id: str) -> None:
        self.session_dir(job_id).mkdir(parents=True, exist_ok=True)
        self.workspace_dir(job_id).mkdir(parents=True, exist_ok=True)
        self.artifact_dir(job_id).mkdir(parents=True, exist_ok=True)


class JobStateStore:
    def __init__(self, layout: JobLayout) -> None:
        self.layout = layout

    def save(self, job: AgentJob) -> None:
        _write_json(self.layout.job_file(job.id), job.to_dict())

    def load(self, job_id: str) -> AgentJob:
        path = self.layout.job_file(job_id)
        if not path.exists():
            raise JobNotFoundError(f"Job not found: {job_id}")
        return AgentJob.from_dict(_read_json(path))

    def list(self) -> list[AgentJob]:
        jobs: list[AgentJob] = []
        for path in sorted(self.layout.jobs_root.glob("*/job.json")):
            jobs.append(AgentJob.from_dict(_read_json(path)))
        return jobs


class SessionStore:
    def __init__(self, layout: JobLayout) -> None:
        self.layout = layout

    def append(self, event: JobEvent) -> None:
        path = self.layout.session_file(event.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def read(self, job_id: str) -> list[JobEvent]:
        path = self.layout.session_file(job_id)
        if not path.exists():
            return []
        events: list[JobEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(JobEvent.from_dict(json.loads(line)))
        return events


class WorkspaceStore:
    def __init__(self, layout: JobLayout) -> None:
        self.layout = layout

    def create(self, job_id: str, source: str | Path | None = None) -> Path:
        workspace_path = self.layout.workspace_dir(job_id)
        workspace_path.mkdir(parents=True, exist_ok=True)
        if source is None:
            return workspace_path

        source_path = Path(source)
        if not source_path.exists():
            raise FileNotFoundError(f"Workspace source does not exist: {source_path}")
        if not source_path.is_dir():
            raise NotADirectoryError(f"Workspace source must be a directory: {source_path}")

        for item in source_path.iterdir():
            target = workspace_path / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
        return workspace_path

    def path(self, job_id: str) -> Path:
        return self.layout.workspace_dir(job_id)


class ArtifactStore:
    def __init__(self, layout: JobLayout) -> None:
        self.layout = layout

    def write_text(self, job_id: str, relative_path: str, content: str) -> Path:
        path = self._safe_artifact_path(job_id, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def list(self, job_id: str) -> list[str]:
        artifact_dir = self.layout.artifact_dir(job_id)
        if not artifact_dir.exists():
            return []
        return [
            str(path.relative_to(artifact_dir)).replace("\\", "/")
            for path in sorted(artifact_dir.rglob("*"))
            if path.is_file()
        ]

    def read_bytes(self, job_id: str, relative_path: str) -> bytes:
        path = self._safe_artifact_path(job_id, relative_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Artifact not found: {relative_path}")
        return path.read_bytes()

    def _safe_artifact_path(self, job_id: str, relative_path: str) -> Path:
        artifact_dir = self.layout.artifact_dir(job_id).resolve()
        path = (artifact_dir / relative_path).resolve()
        if artifact_dir != path and artifact_dir not in path.parents:
            raise ValueError(f"Artifact path escapes artifact directory: {relative_path}")
        return path


class LocalStores:
    def __init__(self, root: str | Path) -> None:
        self.layout = JobLayout(root)
        self.jobs = JobStateStore(self.layout)
        self.sessions = SessionStore(self.layout)
        self.workspaces = WorkspaceStore(self.layout)
        self.artifacts = ArtifactStore(self.layout)

    def ensure_job(self, job_id: str) -> None:
        self.layout.ensure_job_dirs(job_id)

    def unfinished_jobs(self) -> Iterable[AgentJob]:
        return (job for job in self.jobs.list() if not job.status.is_terminal)
