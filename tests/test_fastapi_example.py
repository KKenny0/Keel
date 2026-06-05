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
