from __future__ import annotations

import importlib.metadata as metadata

import pytest


def _major(version: str) -> int:
    return int(version.split(".", maxsplit=1)[0])


def test_fastapi_example_lists_jobs() -> None:
    pytest.importorskip("fastapi")
    fastapi_version = metadata.version("fastapi")
    starlette_version = metadata.version("starlette")
    if _major(fastapi_version) == 0 and _major(starlette_version) >= 1:
        pytest.skip("installed FastAPI and Starlette versions are incompatible")

    testclient = pytest.importorskip("fastapi.testclient")
    app_module = pytest.importorskip("examples.fastapi_app.app")

    response = testclient.TestClient(app_module.app).get("/jobs")

    assert response.status_code == 200
    assert response.json() == []


def test_fastapi_example_forwards_task_dependencies(monkeypatch) -> None:
    pytest.importorskip("fastapi")
    fastapi_version = metadata.version("fastapi")
    starlette_version = metadata.version("starlette")
    if _major(fastapi_version) == 0 and _major(starlette_version) >= 1:
        pytest.skip("installed FastAPI and Starlette versions are incompatible")

    testclient = pytest.importorskip("fastapi.testclient")
    app_module = pytest.importorskip("examples.fastapi_app.app")

    class FakeManager:
        def __init__(self) -> None:
            self.request = None

        def create_job(self, spec, input, workspace=None, dependencies=None, artifact_inputs=None):
            self.request = {
                "spec": spec.to_dict(),
                "input": input,
                "workspace": workspace,
                "dependencies": dependencies,
                "artifact_inputs": artifact_inputs,
            }
            return "job-1"

    fake_manager = FakeManager()
    monkeypatch.setattr(app_module, "manager", fake_manager)

    response = testclient.TestClient(app_module.app).post(
        "/jobs",
        json={
            "spec": {"name": "consumer"},
            "input": {"task": "consume"},
            "dependencies": ["job-0"],
            "artifact_inputs": [
                {
                    "source_job_id": "job-0",
                    "source_path": "result.txt",
                    "target_path": "inputs/result.txt",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"job_id": "job-1"}
    assert fake_manager.request["dependencies"] == ["job-0"]
    assert fake_manager.request["artifact_inputs"][0]["target_path"] == "inputs/result.txt"
