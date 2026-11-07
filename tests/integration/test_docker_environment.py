from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evocode.agent.tools import ToolExecutor
from evocode.config import AppConfig
from evocode.contracts import TaskSpec, ToolCall
from evocode.persistence import EventStore

ROOT = Path(__file__).resolve().parents[2]


def test_offline_dependency_install_has_actionable_message(tmp_path):
    cfg = AppConfig(data_dir=str(tmp_path / "data"))
    cfg.runtime.execution_backend = "docker"
    executor = ToolExecutor(None, cfg)
    assert executor._is_dependency_install(["pip", "install", "fastapi"])
    assert executor._is_dependency_install(["python", "-m", "pip", "install", "pytest"])
    assert executor._is_dependency_install(["uv", "sync"])
    assert not executor._is_dependency_install(["python", "-m", "pytest"])

    repo = tmp_path / "repo"
    repo.mkdir()
    store = EventStore(cfg.sqlite_path)
    executor = ToolExecutor(store, cfg)
    task = store.create_task(TaskSpec(repo_path=str(repo), prompt="install package"), cfg.snapshot())
    try:
        result = asyncio.run(
            executor.execute(
                task,
                ToolCall(id="pip-install", name="run_command", arguments={"command": ["pip", "install", "fastapi"]}),
            )
        )
    finally:
        store.close()
    assert result.status == "failed"
    assert "--network none" in result.output
    assert "prepare_docker_environment.py" in result.output


@pytest.mark.skipif(
    os.getenv("EVOCODE_DOCKER_TESTS") != "1",
    reason="set EVOCODE_DOCKER_TESTS=1 to build and run the Docker integration check",
)
def test_prepared_image_runs_fastapi_testclient_without_tool_network(tmp_path):
    image = os.getenv("EVOCODE_DOCKER_IMAGE", "evocode-runtime:py312")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/prepare_docker_environment.py"), "--tag", image],
        cwd=ROOT,
        check=True,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(data_dir=str(tmp_path / "data"))
    cfg.runtime.execution_backend = "docker"
    cfg.runtime.docker_image = image
    store = EventStore(cfg.sqlite_path)
    task = store.create_task(TaskSpec(repo_path=str(repo), prompt="check prepared runtime"), cfg.snapshot())
    code = (
        "import fastapi, httpx, pytest, uvicorn; "
        "from fastapi import FastAPI; from fastapi.testclient import TestClient; "
        "app = FastAPI(); app.get('/health')(lambda: {'status': 'ok'}); "
        "response = TestClient(app).get('/health'); "
        "print(response.status_code, response.json()); "
        "assert response.status_code == 200 and response.json() == {'status': 'ok'}"
    )
    try:
        result = asyncio.run(
            ToolExecutor(store, cfg).execute(
                task,
                ToolCall(id="docker-health", name="run_command", arguments={"command": ["python", "-c", code]}),
            )
        )
        assert result.status == "completed", result.output
        assert "200 {'status': 'ok'}" in result.output
    finally:
        store.close()
