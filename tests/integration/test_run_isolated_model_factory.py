import hashlib
import json
from types import SimpleNamespace

import pytest

from evocode.app import Application
from evocode.config import AppConfig
from evocode.skills import SkillRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("frozen_collection", [None, "rawrag_eval_frozen_fixture"])
async def test_run_isolated_uses_scoped_model_factory_for_branch(tmp_path, monkeypatch, frozen_collection):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AppConfig(data_dir=str(tmp_path / "app-data"))
    app = Application(config)
    factory_configs = []
    model_calls = []
    automatic_queries = []

    class MeteredModel:
        async def complete(self, messages, *, role, **kwargs):
            model_calls.append({"role": role, "messages": messages, "kwargs": kwargs})
            return SimpleNamespace(content='{"action":"respond","answer":"ok"}', usage={"total_tokens": 7})

    class FakeController:
        def __init__(self, store, llm, branch_config, memory, registry, *, automatic_memory_query=None):
            self.store = store
            self.llm = llm
            automatic_queries.append(automatic_memory_query)

        async def run(self, task_id):
            await self.llm.complete([{"role": "user", "content": "hello"}], role="planner")
            self.store.append_event(task_id, "assistant.final", {"content": "ok"})
            return {"status": "completed"}

    monkeypatch.setattr("evocode.agent.AgentController", FakeController)

    def factory(branch_config):
        factory_configs.append(branch_config)
        return MeteredModel()

    try:
        result = await app.run_isolated(
            {"repo_path": str(workspace), "prompt": "hello"},
            {"config": config.snapshot()},
            str(workspace),
            llm_factory=factory,
            frozen_memory_collection=frozen_collection,
            automatic_memory_query="current task query",
        )
    finally:
        await app.close()

    assert result["result"] == {"status": "completed"}
    assert len(factory_configs) == 1
    assert factory_configs[0].data_dir != config.data_dir
    assert "/evaluations/" in factory_configs[0].data_dir
    if frozen_collection:
        assert factory_configs[0].raw_rag.collection == frozen_collection
        assert factory_configs[0].raw_rag.background_indexing is False
    else:
        assert factory_configs[0].raw_rag.collection.startswith("eval_")
    assert model_calls and model_calls[0]["role"] == "planner"
    assert automatic_queries == ["current task query"]


async def test_run_isolated_rejects_symlinked_oracle_test(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    tests = workspace / "tests"
    tests.mkdir(parents=True)
    (workspace / "redirected.py").write_text("original\n")
    (tests / "protected.py").symlink_to("../redirected.py")
    config = AppConfig(data_dir=str(tmp_path / "app-data"))

    class FakeController:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, _task_id):
            return {"status": "completed"}

    monkeypatch.setattr("evocode.agent.AgentController", FakeController)
    app = Application(config)
    try:
        with pytest.raises(ValueError, match="redirected by a symlink"):
            await app.run_isolated(
                {
                    "repo_path": str(workspace),
                    "prompt": "check",
                    "acceptance_command": ["python", "tests/protected.py"],
                },
                {"acceptance_files": {"tests/protected.py": "assert False\n"}},
                str(workspace),
                llm_factory=lambda _config: object(),
            )
    finally:
        await app.close()
    assert (workspace / "redirected.py").read_text() == "original\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected_count", [("valid", 1), ("bad_hash", 0), ("no_usage", 0)])
async def test_run_isolated_reports_only_hash_verified_coder_model_injections(
    tmp_path, monkeypatch, mode, expected_count,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AppConfig(data_dir=str(tmp_path / "app-data"))
    source_content = "Use exact boundaries and preserve the existing response fields."
    source_version = {
        "version_id": "source-version-id",
        "skill_id": "source-skill-id",
        "name": "versioned instructions",
        "level": "project",
        "scope_id": "fixture-repository",
        "content": source_content,
        "content_sha256": hashlib.sha256(source_content.encode()).hexdigest(),
        "status": "draft",
        "metadata": {},
    }

    class ContextController:
        def __init__(self, store, _llm, branch_config, _memory, _skills, **_kwargs):
            self.store = store
            self.branch_config = branch_config

        async def run(self, task_id):
            registry = SkillRegistry(self.store, self.branch_config.data_dir)
            version = next(item for item in registry.list_versions()
                           if item["level"] == "project" and item["status"] == "active")
            context_id = "coder-context"
            skill_item = {
                "version_id": version["version_id"],
                "skill_id": version["skill_id"],
                "name": version["name"],
                "content": version["content"],
                "content_sha256": version["content_sha256"],
            }
            if mode == "bad_hash":
                skill_item["content_sha256"] = "0" * 64
            context = {
                "context_id": context_id,
                "node": "coder",
                "messages": [{
                    "role": "user",
                    "content": "Applicable versioned skills (subject to task constraints):\n"
                    + json.dumps([skill_item]),
                }],
            }
            artifact = self.store.save_artifact(json.dumps(context), kind="node_context")
            self.store.append_event(task_id, "context.assembled", {
                "context_id": context_id,
                "role": "coder",
                "node": "coder",
                "artifact_id": artifact["artifact_id"],
            })
            usage_context = "some-other-context" if mode == "no_usage" else context_id
            self.store.append_event(task_id, "model.usage", {
                "context_id": usage_context,
                "role": "coder",
                "provider_total_tokens": 10,
            })
            return {"status": "completed"}

    monkeypatch.setattr("evocode.agent.AgentController", ContextController)
    app = Application(config)
    try:
        result = await app.run_isolated(
            {"repo_path": str(workspace), "prompt": "use the skill", "repository_id": "fixture-repository"},
            {"config": config.snapshot(), "skill_versions": [source_version]},
            str(workspace),
            llm_factory=lambda _config: object(),
        )
    finally:
        await app.close()

    evidence = result["coder_skill_injection_evidence"]
    assert len(evidence) == expected_count
    if evidence:
        assert evidence[0]["content_sha256"] == source_version["content_sha256"]
        assert evidence[0]["version_id"] != source_version["version_id"]
        assert evidence[0]["injection_verified"] is True
        assert evidence[0]["model_call_observed"] is True
