"""Exercise grader isolation and failure admission using real subprocesses."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.dataset_validation import copy_clean, execute, inventory, validate_stage


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def fixture(root):
    public = "import unittest\nfrom app import value\nclass Public(unittest.TestCase):\n def test_public(self): self.assertEqual(value(1), 1)\n"
    hidden = "import unittest\nfrom app import value\nclass Hidden(unittest.TestCase):\n def test_hidden(self): self.assertEqual(value(-1), 0)\n"
    regression = "import unittest\nfrom app import value\nclass Regression(unittest.TestCase):\n def test_regression(self): self.assertEqual(value(2), 2)\n"
    write(root, "scenario/code/app.py", "def value(x): return x\n")
    write(root, "scenario/code/tests/test_public.py", public)
    write(root, "private/reference/app.py", "def value(x): return max(0, x)\n")
    write(root, "private/reference/tests/test_public.py", public)
    write(root, "private/acceptance/test_hidden.py", hidden)
    write(root, "private/regression/test_regression.py", regression)
    write(root, "scenario/task.md", "Clamp negatives to zero.")
    bad = []
    for category in ("swallow_exception", "hardcode", "remove_validation"):
        overlay = "private/bad/" + category
        write(root, overlay + "/app.py", "def value(x): return x\n")
        write(root, overlay + "/tests/test_public.py", public)
        bad.append({"name": category, "category": category, "overlay_dir": overlay})
    manifest = {"task_id": "test_t1", "repository_id": "repo_api", "stage": "seed",
                "code_dir": "scenario/code", "task_file": "scenario/task.md",
                "private_tests_dir": "private/acceptance", "regression_tests_dir": "private/regression",
                "reference_dir": "private/reference", "representative_bad_patches": bad,
                "acceptance_command": ["python", "-m", "unittest", "discover", "-s", "tests"]}
    write(root, "scenarios/repo_api/t1_seed/manifest.json", json.dumps(manifest))
    return manifest


def test_admission_runs_independent_clean_grader_copies(tmp_path):
    fixture(tmp_path)
    before = inventory(tmp_path / "scenario/code")
    report = validate_stage(tmp_path, tmp_path / ".runtime/check", "t1_seed", sys.executable)
    assert report["passed"]
    assert report["baseline"]["acceptance"]["exit_code"] == 1
    assert report["reference"]["combined"]["tests_run"] == 3
    assert all(item["rejected"] for item in report["bad_patches"])
    assert before == inventory(tmp_path / "scenario/code")
    assert not (tmp_path / "scenario/code/_grader_acceptance").exists()
    assert len({x["cwd"] for x in report["baseline"].values()}) == 4
    assert Path(report["baseline"]["acceptance"]["log"]).is_file()


def test_zero_tests_cannot_admit_target_failure(tmp_path):
    fixture(tmp_path)
    (tmp_path / "private/acceptance/test_hidden.py").write_text("# no tests\n")
    report = validate_stage(tmp_path, tmp_path / ".runtime/check", "t1_seed", sys.executable)
    assert not report["passed"]
    assert not report["checks"]["baseline_target_fails"]


def test_broken_hidden_import_is_infrastructure_not_target_failure(tmp_path):
    fixture(tmp_path)
    (tmp_path / "private/acceptance/test_hidden.py").write_text("import nonexistent_offline_dependency\n")
    report = validate_stage(tmp_path, tmp_path / ".runtime/check", "t1_seed", sys.executable)
    assert not report["checks"]["baseline_target_fails"]
    assert report["baseline"]["acceptance"]["test_errors"] == 1


def test_private_test_name_collision_rejected(tmp_path):
    fixture(tmp_path)
    write(tmp_path, "private/acceptance/test_public.py", "# collision\n")
    with pytest.raises(ValueError, match="collision"):
        validate_stage(tmp_path, tmp_path / ".runtime/check", "t1_seed", sys.executable)


def test_clean_copy_omits_caches_rejects_symlinks(tmp_path):
    write(tmp_path, "source/app.py", "print(1)\n")
    write(tmp_path, "source/__pycache__/app.pyc", "old cache")
    copy_clean(tmp_path / "source", tmp_path / "copy")
    assert not (tmp_path / "copy/__pycache__").exists()
    (tmp_path / "source/link").symlink_to(tmp_path / "source/app.py")
    with pytest.raises(ValueError, match="symlinks"):
        copy_clean(tmp_path / "source", tmp_path / "copy2")


def test_command_timeout_keeps_exit_and_log(tmp_path):
    result = execute([sys.executable, "-c", "import time; print('started', flush=True); time.sleep(3)"],
                     tmp_path, tmp_path / "timeout.log", timeout=0.05)
    assert result["timed_out"]
    assert result["exit_code"] < 0
    assert "started" in (tmp_path / "timeout.log").read_text()
