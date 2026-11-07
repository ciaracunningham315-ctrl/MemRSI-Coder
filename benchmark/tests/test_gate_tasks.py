"""Independent Gate fixtures, provenance and oracle-free materialization."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from runner import gate_tasks

CATALOG = json.loads((ROOT / gate_tasks.CATALOG).read_text())


@pytest.fixture
def runtime():
    (ROOT / ".runtime").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="test_gates_", dir=ROOT / ".runtime") as directory:
        yield Path(directory)


def test_catalog_ids_and_independent_sources():
    api_ids = {task_id for task_id, repository_id in CATALOG["repository_ids"].items()
               if repository_id == "repo_api"}
    cli_ids = {task_id for task_id, repository_id in CATALOG["repository_ids"].items()
               if repository_id == "repo_cli"}
    data_ids = {task_id for task_id, repository_id in CATALOG["repository_ids"].items()
                if repository_id == "repo_data"}
    assert len(api_ids) == 5
    assert len(cli_ids) == 5
    assert len(data_ids) == 5
    for task_id, relative in CATALOG["tasks"].items():
        manifest = gate_tasks.load_gate_manifest(ROOT, task_id)
        assert (ROOT / relative).is_file()
        assert manifest["repository_id"] == CATALOG["repository_ids"][task_id]
        assert manifest["future_main_inputs_used"] == []
        assert not any(str(manifest[key]).startswith(("scenarios/", "private/acceptance/", "private/regression/"))
                       for key in gate_tasks.PATH_KEYS)


def test_real_offline_admission_preserves_logs():
    report = gate_tasks.validate_gate_tasks(ROOT, persist_evidence=False)
    assert report["passed"]
    assert report["model_calls"] == 0
    assert report["formal_results"] is False
    for task in report["tasks"]:
        assert all(task["checks"].values())
        assert task["commands"]["baseline"]["target"]["test_failures"] > 0
        for variant in task["commands"].values():
            for command in variant.values():
                assert (ROOT / command["log"]).is_file()
                assert command["tests_run"] > 0
                assert not command["timed_out"]


def test_meta_reads_only_its_own_manifest_and_no_future_inputs(runtime):
    original = gate_tasks.load_gate_manifest
    seen = []
    def record(root, task_id):
        seen.append(task_id)
        return original(root, task_id)
    with patch.object(gate_tasks, "load_gate_manifest", side_effect=record):
        specs = gate_tasks.build_gate_specs(ROOT, "transfer", "meta", runtime)
    # Scoped selection verifies the catalog's repository provenance before
    # materializing the chosen manifest, so the same trusted manifest can be
    # loaded once for filtering and once for checkout creation.
    assert set(seen) == {"repo_api_gate_meta_reminder"}
    assert specs[0]["task_id"] == "repo_api_gate_meta_reminder"
    assert specs[0]["repository_id"] == "repo_api"
    assert "hold" not in specs[0]["prompt"].lower()


@pytest.mark.parametrize("stage,task_id", [
    ("seed", "repo_api_gate_seed_print"),
    ("transfer", "repo_api_gate_transfer_export"),
    ("stale", "repo_api_gate_stale_notify"),
])
def test_only_public_tree_in_single_commit_checkout(runtime, stage, task_id):
    [spec] = gate_tasks.build_gate_specs(ROOT, stage, "task", runtime)
    assert spec["task_id"] == task_id
    workspace = Path(spec["repo_path"])
    manifest = gate_tasks.load_gate_manifest(ROOT, task_id)
    assert gate_tasks.inventory(workspace) == gate_tasks.inventory(ROOT / manifest["code_dir"])
    assert subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=workspace, text=True).strip() == "1"
    assert subprocess.check_output(["git", "remote"], cwd=workspace, text=True) == ""
    assert not any(path.name.startswith("test_private") for path in workspace.rglob("*"))
    assert not (workspace / "reference").exists()
    for target, oracle in spec["protected_acceptance_paths"].items():
        assert Path(oracle).is_file()
        assert not Path(oracle).is_relative_to(workspace)
        assert target.startswith("tests/")
    assert "reference" not in json.dumps(spec)
    assert all("private" not in item and "reference" not in item for item in spec["acceptance_command"])


def test_fixed_replay_available_at_all_learning_boundaries(runtime):
    for stage in ("seed", "transfer", "stale"):
        [spec] = gate_tasks.build_gate_specs(ROOT, stage, "regression", runtime)
        assert spec["task_id"] == "repo_api_replay_status"
        assert spec["stage"] == "seed"


def test_future_gate_rejected_before_reading_its_source(runtime):
    with patch.object(gate_tasks, "load_gate_manifest", side_effect=AssertionError("future source read")):
        with pytest.raises(ValueError, match="not available"):
            gate_tasks.materialize_gate_spec(ROOT, "repo_api_gate_stale_notify", runtime, stage="transfer")
    for stage in ("seed", "stale", "heldout"):
        with pytest.raises(ValueError):
            gate_tasks.build_gate_specs(ROOT, stage, "meta", runtime)
    with pytest.raises(ValueError):
        gate_tasks.build_gate_specs(ROOT, "heldout", "task", runtime)


def test_manifest_tampering_fails_closed(tmp_path):
    task_id = "repo_api_gate_seed_print"
    path = Path(CATALOG["tasks"][task_id])
    (tmp_path / "configs").mkdir()
    shutil.copyfile(ROOT / gate_tasks.CATALOG, tmp_path / gate_tasks.CATALOG)
    shutil.copytree((ROOT / path).parent, (tmp_path / path).parent)
    code = (tmp_path / path).parent / "code/api/service.py"
    code.write_text(code.read_text() + "\n# changed after freeze\n")
    with pytest.raises(ValueError, match="changed"):
        gate_tasks.load_gate_manifest(tmp_path, task_id)


def test_workspace_must_stay_under_runtime(tmp_path):
    with pytest.raises(ValueError, match="benchmark/.runtime"):
        gate_tasks.materialize_gate_spec(ROOT, "repo_api_gate_seed_print", tmp_path, stage="seed")
