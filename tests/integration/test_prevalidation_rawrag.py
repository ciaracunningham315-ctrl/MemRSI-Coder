"""Offline integrity and protocol checks for Raw-RAG held-out fixtures."""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "prevalidation" / "rawrag"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live_memory_ablation.py"
_SPEC = importlib.util.spec_from_file_location("live_memory_ablation_prevalidation", SCRIPT)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def _run_acceptance(workspace: Path, task_dir: Path) -> subprocess.CompletedProcess:
    protected = task_dir / "acceptance.py"
    destination = workspace / "tests" / "prevalidation_acceptance.py"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(protected, destination)
    return subprocess.run(
        [sys.executable, "-I", "-B", str(destination)],
        cwd=workspace, capture_output=True, text=True, check=False,
    )


def test_baselines_fail_and_reference_fixes_pass_for_all_held_out_tasks(tmp_path):
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["task_count"] == 4
    assert len(manifest["tasks"]) == 4
    assert "public EvoCode API" in manifest["provenance"]
    assert "--collect-training-task-id" not in manifest["provenance"]

    for entry in manifest["tasks"]:
        task_dir = ROOT / entry["task_spec"].rsplit("/", 1)[0]
        spec = json.loads((ROOT / entry["task_spec"]).read_text(encoding="utf-8"))
        assert "training_trace_policy" not in spec
        assert spec["training_prompt"] == entry["training_prompt"]
        assert (ROOT / entry["training_workspace"]).is_dir()
        assert entry["training_scope"] in {"same_project_mapped", "foreign_scope_unmapped"}

        for side, expected_returncode in (("workspace", 1), ("reference_workspace", 0)):
            source = ROOT / entry[side]
            candidate = tmp_path / f"{entry['task_id']}-{side}"
            shutil.copytree(source, candidate)
            result = _run_acceptance(candidate, task_dir)
            assert result.returncode == expected_returncode, (
                f"{entry['task_id']} {side} acceptance returned {result.returncode}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )


def test_runner_public_protocol_rejects_authored_trace_cli():
    with pytest.raises(SystemExit) as error:
        _MODULE.main(["--execute", "--trace", "/tmp/trace.json"])
    assert error.value.code == 2


def test_runner_manifest_preserves_source_and_held_out_scope_identity():
    runner_path = SCRIPT.with_name("rawrag_blackbox.py")
    spec = importlib.util.spec_from_file_location("rawrag_blackbox_manifest", runner_path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    manifest, _, tasks = runner.read_manifest(ROOT / "manifest.json")
    assert len(tasks) == manifest["task_count"]
    for task in tasks:
        source_id = runner.repository_id(task["_training_workspace"])
        held_out_id = runner.repository_id(task["_workspace"])
        if task["training_scope"] == "same_project_mapped":
            assert source_id != held_out_id
        else:
            assert task["training_scope"] == "foreign_scope_unmapped"
            assert source_id != held_out_id


@pytest.mark.asyncio
async def test_real_fts_memory_keeps_foreign_history_out_of_new_session(tmp_path):
    """Exercise the actual MemoryService with local SQLite FTS and no provider calls."""
    from evocode.app import Application
    from evocode.config import AppConfig
    from evocode.contracts import TaskSpec

    target_workspace = tmp_path / "target-workspace"
    foreign_workspace = tmp_path / "foreign-workspace"
    target_workspace.mkdir()
    foreign_workspace.mkdir()
    config = AppConfig(data_dir=str(tmp_path / "state"))
    config.raw_rag.enabled = True
    config.raw_rag.qdrant_url = ""
    config.raw_rag.embedding_model = ""
    config.raw_rag.rerank_enabled = False
    config.raw_rag.background_indexing = False
    config.evolution.enabled = False
    config.skills.enabled = False
    app = Application(config)
    try:
        target_history = app.create_task(TaskSpec(
            repo_path=str(target_workspace), prompt="Earlier target repository task", repository_id="target",
        ))
        foreign_history = app.create_task(TaskSpec(
            repo_path=str(foreign_workspace), prompt="Earlier foreign repository task", repository_id="foreign",
        ))
        current = app.create_task(TaskSpec(
            repo_path=str(target_workspace), prompt="New session", repository_id="target",
        ))
        app.store.append_event(target_history["task_id"], "test.result", {
            "passed": True, "output": "targetmarker retry header normalization",
        })
        app.store.append_event(foreign_history["task_id"], "test.result", {
            "passed": True, "output": "foreignmarker retry header normalization",
        })
        index = await app.memory.index_pending(limit=20)
        assert index["status"] == "degraded"
        assert app.memory.status()["chunks"] > 0

        recalled = await app.memory.search("retry header normalization", current)
        assert recalled.mode == "fts"
        assert recalled.evidence
        assert any("targetmarker" in evidence.text for evidence in recalled.evidence)
        assert all("foreignmarker" not in evidence.text for evidence in recalled.evidence)
        assert all(evidence.session_id != current["session_id"] for evidence in recalled.evidence)
    finally:
        await app.close()
