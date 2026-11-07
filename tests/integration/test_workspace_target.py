import asyncio
import json
from pathlib import Path

from evocode.app import Application
from evocode.config import AppConfig
from evocode.context import ContextAssembler
from evocode.contracts import TaskSpec, WorkspaceMapping


class UnusedModel:
    async def complete(self, *args, **kwargs):  # pragma: no cover - context assembly must not call the model
        raise AssertionError("Unexpected model call")


def test_context_uses_selected_workspace_mapping_even_when_prompt_mentions_sibling(tmp_path):
    selected = tmp_path / "text1"
    requested = tmp_path / "text2"
    selected.mkdir()
    requested.mkdir()

    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.raw_rag.enabled = False
    config.skills.enabled = False
    config.evolution.enabled = False
    config.runtime.execution_backend = "docker"
    app = Application(config, UnusedModel())
    try:
        task = app.create_task(TaskSpec(
            repo_path=str(selected),
            prompt=f"Create a FastAPI service in {requested}.",
        ))
        messages = asyncio.run(ContextAssembler(app.store, app.llm, config).build(task, "coder", []))
        context_message = next(
            item["content"] for item in messages
            if item.get("content", "").startswith("Current task context (authoritative current requirements):")
        )
        context = json.loads(context_message.split("\n", 1)[1])
        expected = {
            "host_root": str(selected.resolve()),
            "execution_root": "/workspace",
            "working_directory": ".",
            "target_source": "selected_session_workspace",
            "target_path": ".",
            "fresh_container_per_command": True,
            "network_available": False,
        }
        assert context["workspace"] == expected
        assert requested.resolve().as_posix() not in json.dumps(context["workspace"])
        assert any(
            "Never infer that an absolute path mentioned in the request is mounted" in item.get("content", "")
            for item in messages
        )
        artifact_id = app.store.read_events(task_id=task["task_id"])[-1]["payload"]["artifact_id"]
        artifact_record = app.store.get_artifact(artifact_id)
        artifact = json.loads(Path(artifact_record["path"]).read_text())
        assert artifact["workspace"] == expected
        assert artifact["workspace"]["target_path"] == "."
    finally:
        asyncio.run(app.close())


def test_workspace_mapping_uses_host_root_for_host_backend(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    mapping = WorkspaceMapping.for_workspace(str(root), "host")

    assert mapping.host_root == str(root.resolve())
    assert mapping.execution_root == str(root.resolve())
    assert mapping.target_path == "."
    assert mapping.fresh_container_per_command is False
    assert mapping.network_available is True
    system_prompt = ContextAssembler(None, None, None)._system_prompt("coder", mapping)
    assert "Commands run directly on the host" in system_prompt
    assert "fresh isolated container with no network" not in system_prompt
