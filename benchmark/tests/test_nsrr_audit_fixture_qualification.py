"""Frozen NSRR fixture manifest, privacy boundary and TaskSpec compatibility."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
EVOCODE_SOURCE = ROOT.parent / "src"
if str(EVOCODE_SOURCE) not in sys.path:
    sys.path.insert(0, str(EVOCODE_SOURCE))

from evocode.contracts import TaskSpec  # noqa: E402
from runner.dataset_validation import inventory  # noqa: E402
from runner.nsrr_audit_tasks import (  # noqa: E402
    ACCEPTANCE_COMMAND,
    MANIFEST_FIELDS,
    TASK_IDS,
    compute_fixed_input_fingerprint,
    load_nsrr_manifest,
    materialize_nsrr_task_spec,
    nsrr_fixture_input_paths,
)


def test_eight_fixture_manifests_cover_distinct_audit_scopes_and_frozen_inputs():
    assert len(TASK_IDS) == 8
    assert len(set(TASK_IDS)) == 8
    for task_id in TASK_IDS:
        manifest = load_nsrr_manifest(ROOT, task_id)
        assert set(MANIFEST_FIELDS) <= manifest.keys()
        assert manifest["repository_id"] == task_id
        assert manifest["acceptance_command"] == ACCEPTANCE_COMMAND
        assert manifest["independent_of_main_tasks"] is True
        assert manifest["main_task_score_eligible"] is False
        assert manifest["audit_use_only"] is True
        assert manifest["fixed_input_fingerprint"] == compute_fixed_input_fingerprint(ROOT, manifest)
        assert len(manifest["fixed_input_fingerprint"]) == 64


def test_prompts_are_business_goals_and_do_not_copy_candidate_guidance_or_oracle_cases():
    for task_id in TASK_IDS:
        manifest = load_nsrr_manifest(ROOT, task_id)
        prompt = (ROOT / manifest["task_file"]).read_text(encoding="utf-8").lower()
        candidate_id = manifest["candidate_id"]
        assert candidate_id not in prompt
        for skill_field in ("baseline_skill", "candidate_skill"):
            skill = (ROOT / manifest[skill_field]).read_text(encoding="utf-8").strip().lower()
            assert skill not in prompt
        cases = json.loads((ROOT / manifest["trusted_tests_file"]).read_text(encoding="utf-8"))["cases"]
        gate_cases = json.loads((ROOT / manifest["private_tests_dir"] / "gate_cases.json").read_text(encoding="utf-8"))["cases"]
        assert "tests/trusted_tests.json" not in manifest["protected_acceptance_paths"]
        assert manifest["trusted_tests_file"] not in manifest["protected_acceptance_paths"].values()
        assert not ({c["id"] for c in cases} & {c["id"] for c in gate_cases})
        assert not (
            {json.dumps(c["input"], sort_keys=True) for c in cases}
            & {json.dumps(c["input"], sort_keys=True) for c in gate_cases}
        )
        for case in [*cases, *gate_cases]:
            assert case["id"].lower() not in prompt
            assert json.dumps(case["expected"], ensure_ascii=False, sort_keys=True).lower() not in prompt


def test_frozen_hash_inventory_includes_prompts_hidden_tests_reference_and_code():
    fixture_inputs = nsrr_fixture_input_paths(ROOT)
    assert len(fixture_inputs) >= 60
    for task_id in TASK_IDS:
        manifest = load_nsrr_manifest(ROOT, task_id)
        assert manifest["task_file"] in fixture_inputs
        assert manifest["trusted_tests_file"] in fixture_inputs
        assert any(path.startswith(manifest["code_dir"] + "/") for path in fixture_inputs)
        assert any(path.startswith(manifest["reference_dir"] + "/") for path in fixture_inputs)
        assert any(path.startswith(manifest["private_tests_dir"] + "/") for path in fixture_inputs)
    assert "runner/nsrr_audit_tasks.py" in fixture_inputs
    assert "scripts/qualify_nsrr_audit_fixtures.py" in fixture_inputs
    assert "tests/test_nsrr_audit_fixture_qualification.py" in fixture_inputs


def test_materialized_spec_matches_sandbox_task_contract_without_oracle_leakage():
    runtime_root = ROOT / ".runtime"
    runtime_root.mkdir(exist_ok=True)
    runtime = runtime_root / ("nsrr_spec_test_" + uuid.uuid4().hex)
    try:
        manifest = load_nsrr_manifest(ROOT, TASK_IDS[0])
        spec = materialize_nsrr_task_spec(ROOT, TASK_IDS[0], runtime, require_qualification=False)
        public_fields = {"repo_path", "prompt", "acceptance_command", "repository_id"}
        assert public_fields <= set(TaskSpec.model_fields)
        task_spec = TaskSpec.model_validate({key: spec[key] for key in public_fields})
        assert task_spec.repository_id == manifest["repository_id"]
        assert task_spec.acceptance_command == ACCEPTANCE_COMMAND
        checkout = Path(task_spec.repo_path)
        assert checkout.is_dir()
        assert inventory(checkout) == inventory(ROOT / manifest["code_dir"])
        assert set(spec["protected_acceptance_paths"]) == set(manifest["protected_acceptance_paths"])
        for target, oracle in spec["protected_acceptance_paths"].items():
            assert target.startswith("tests/")
            assert Path(oracle).is_file()
            assert not Path(oracle).is_relative_to(checkout)
        assert "reference_dir" not in spec
        assert str(ROOT / manifest["reference_dir"]) not in json.dumps(spec)
        assert "expected" not in task_spec.prompt.lower()
    finally:
        shutil.rmtree(runtime, ignore_errors=True)
