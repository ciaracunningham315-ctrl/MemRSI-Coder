"""Trusted NSRR sandbox fixture manifests and offline qualification.

These fixtures are an independent audit instrument. They are deliberately not
registered in the formal main-task catalog and never contribute to its scores.
Only the host-side materialized spec contains protected oracle paths; the public
TaskSpec fields are compatible with ``evocode.contracts.TaskSpec``.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

from runner.dataset_validation import copy_clean, execute, inventory, passed, completed, source_path


TASKS_ROOT = Path("private/gate_audit/tasks")
FIXTURE_VERSION = "nsrr-audit-sandbox-fixture-v1"
ACCEPTANCE_COMMAND = ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
TASK_IDS = (
    "nsrr_audit_benefit_cache",
    "nsrr_audit_benefit_decimal",
    "nsrr_audit_dup_config_null",
    "nsrr_audit_dup_headers",
    "nsrr_audit_harm_error_swallow",
    "nsrr_audit_harm_idempotency",
    "nsrr_audit_stale_retry",
    "nsrr_audit_stale_timezone",
)
MANIFEST_FIELDS = (
    "audit_task_id", "candidate_id", "repository_id", "code_dir", "repo_template", "commit", "prompt",
    "reference_dir", "private_tests_dir", "task_file", "trusted_tests_file",
    "acceptance_command", "acceptance_files", "protected_acceptance_paths", "fixed_input_fingerprint",
)
IGNORED = {".git", "__pycache__", ".pytest_cache", ".venv", ".DS_Store"}


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _task_dir(root: Path, task_id: str) -> Path:
    root = Path(root).resolve()
    if task_id not in TASK_IDS:
        raise ValueError(f"unknown frozen NSRR audit task: {task_id}")
    return source_path(root, (TASKS_ROOT / task_id).as_posix())


def _manifest_input_paths(root: Path, manifest: dict) -> dict[str, Path]:
    task_dir = _task_dir(root, manifest["audit_task_id"])
    paths: dict[str, Path] = {}
    for field in ("code_dir", "reference_dir", "private_tests_dir"):
        source = source_path(root, manifest[field])
        if not source.is_relative_to(task_dir):
            raise ValueError(f"{field} must stay within the independent audit task")
        for relative in inventory(source):
            paths[f"{manifest[field]}/{relative}"] = source / relative
    for field in ("task_file", "trusted_tests_file"):
        path = source_path(root, manifest[field])
        if not path.is_relative_to(task_dir):
            raise ValueError(f"{field} must stay within the independent audit task")
        paths[manifest[field]] = path
    task_json = source_path(root, (TASKS_ROOT / manifest["audit_task_id"] / "task.json").as_posix())
    paths[task_json.relative_to(root).as_posix()] = task_json
    candidate_json = source_path(root, f"gate_cases/{manifest['candidate_id']}/candidate.json")
    paths[candidate_json.relative_to(root).as_posix()] = candidate_json
    for field in ("baseline_skill", "candidate_skill"):
        skill_path = manifest.get(field)
        if not isinstance(skill_path, str) or not skill_path.startswith(f"gate_cases/{manifest['candidate_id']}/"):
            raise ValueError(f"{field} must point into its frozen candidate directory")
        path = source_path(root, skill_path)
        if not path.is_file():
            raise ValueError(f"{field} must identify a file")
        paths[skill_path] = path
    return dict(sorted(paths.items()))


def compute_fixed_input_fingerprint(root: Path, manifest: dict) -> str:
    """Hash task contract metadata plus every public, hidden, and reference input."""
    root = Path(root).resolve()
    inputs = {relative: _sha256(path) for relative, path in _manifest_input_paths(root, manifest).items()}
    contract = {key: value for key, value in manifest.items() if key != "fixed_input_fingerprint"}
    return hashlib.sha256(_canonical_json({"contract": contract, "inputs": inputs})).hexdigest()


def load_nsrr_manifest(root: Path, task_id: str, *, verify_fingerprint: bool = True) -> dict:
    root = Path(root).resolve()
    task_dir = _task_dir(root, task_id)
    manifest_path = task_dir / "fixture_manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError(f"fixture manifest is missing or unsafe: {task_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    missing = [field for field in MANIFEST_FIELDS if field not in manifest]
    if missing:
        raise ValueError(f"fixture manifest missing fields: {', '.join(missing)}")
    if (manifest.get("fixture_version") != FIXTURE_VERSION
            or manifest.get("audit_task_id") != task_id
            or manifest.get("repository_id") != task_id
            or not re.fullmatch(r"[a-z][a-z0-9_]*", manifest["repository_id"])):
        raise ValueError(f"fixture identity or version mismatch: {task_id}")
    if (manifest.get("independent_of_main_tasks") is not True
            or manifest.get("main_task_score_eligible") is not False
            or manifest.get("audit_use_only") is not True):
        raise ValueError("NSRR fixture provenance must explicitly exclude main-task scoring")
    if manifest["acceptance_command"] != ACCEPTANCE_COMMAND:
        raise ValueError("unsupported independent NSRR acceptance command")
    if manifest["repo_template"] != manifest["code_dir"]:
        raise ValueError("repo_template must alias the frozen code_dir")
    if not re.fullmatch(r"[0-9a-f]{40}", manifest["commit"]):
        raise ValueError("fixture commit must be a pinned Git SHA")
    for field in ("code_dir", "reference_dir", "private_tests_dir", "task_file", "trusted_tests_file"):
        source_path(root, manifest[field])
    for target, relative in manifest["protected_acceptance_paths"].items():
        target_path = Path(target)
        if (not isinstance(relative, str) or target_path.is_absolute() or ".." in target_path.parts
                or "tests" not in target_path.parts
                or (not target_path.name.startswith("test_") and target != "tests/gate_cases.json")):
            raise ValueError("invalid protected NSRR acceptance path")
        if source_path(root, relative).parent != source_path(root, manifest["private_tests_dir"]):
            raise ValueError("hidden NSRR tests must live under private_tests_dir")
        if relative == manifest["trusted_tests_file"] or target == "tests/trusted_tests.json":
            raise ValueError("NSRR Gate protected acceptance must not reuse independent audit trusted_tests.json")
    if sorted(manifest["acceptance_files"]) != sorted(manifest["protected_acceptance_paths"]):
        raise ValueError("acceptance_files must enumerate the protected in-repo test paths")
    task_prompt = source_path(root, manifest["task_file"]).read_text(encoding="utf-8")
    if manifest["prompt"] != task_prompt:
        raise ValueError("manifest prompt must be identical to the frozen task_file")
    task = json.loads(source_path(root, f"{(TASKS_ROOT / task_id / 'task.json').as_posix()}").read_text(encoding="utf-8"))
    tests = json.loads(source_path(root, manifest["trusted_tests_file"]).read_text(encoding="utf-8"))
    gate_cases_path = source_path(root, manifest["private_tests_dir"]) / "gate_cases.json"
    if not gate_cases_path.is_file():
        raise ValueError("NSRR Gate acceptance cases file is missing")
    gate_tests = json.loads(gate_cases_path.read_text(encoding="utf-8"))
    expected_candidate = task_id.replace("nsrr_audit_", "nsrr_", 1)
    if (task.get("audit_task_id") != task_id or tests.get("task_id") != task_id
            or gate_tests.get("task_id") != task_id
            or task.get("candidate_id") != expected_candidate
            or manifest["candidate_id"] != expected_candidate):
        raise ValueError("task metadata and trusted-case IDs differ")
    if not isinstance(tests.get("cases"), list) or len(tests["cases"]) < task.get("minimum_cases", 1):
        raise ValueError("trusted NSRR cases do not meet the task minimum")
    if not isinstance(gate_tests.get("cases"), list) or len(gate_tests["cases"]) < task.get("minimum_cases", 1):
        raise ValueError("NSRR Gate cases do not meet the task minimum")
    audit_ids = {case["id"] for case in tests["cases"]}
    gate_ids = {case["id"] for case in gate_tests["cases"]}
    audit_inputs = {_canonical_json(case["input"]) for case in tests["cases"]}
    gate_inputs = {_canonical_json(case["input"]) for case in gate_tests["cases"]}
    if audit_ids & gate_ids or audit_inputs & gate_inputs:
        raise ValueError("NSRR Gate cases must be disjoint from independent audit trusted_tests.json")
    if verify_fingerprint and manifest["fixed_input_fingerprint"] != compute_fixed_input_fingerprint(root, manifest):
        raise ValueError("NSRR fixture input fingerprint changed; re-freeze and requalify")
    return manifest


def nsrr_fixture_input_paths(root: Path) -> dict[str, Path]:
    """Enumerate immutable fixture and qualification implementation inputs."""
    root = Path(root).resolve()
    paths: dict[str, Path] = {}
    for task_id in TASK_IDS:
        task_dir = _task_dir(root, task_id)
        manifest_path = task_dir / "fixture_manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"NSRR sandbox fixture manifest missing: {task_id}")
        manifest = load_nsrr_manifest(root, task_id)
        paths[manifest_path.relative_to(root).as_posix()] = manifest_path
        paths.update({key: value for key, value in _manifest_input_paths(root, manifest).items()})
    for relative in ("runner/nsrr_audit_tasks.py", "scripts/qualify_nsrr_audit_fixtures.py",
                     "tests/test_nsrr_audit_fixture_qualification.py"):
        path = source_path(root, relative)
        paths[relative] = path
    return dict(sorted(paths.items()))


def materialize_nsrr_task_spec(root: Path, task_id: str, runtime_dir: Path, *, require_qualification: bool = True) -> dict:
    """Build a clean one-commit source checkout and host-only protected oracle map."""
    root = Path(root).resolve()
    manifest = load_nsrr_manifest(root, task_id)
    task_dir = _task_dir(root, task_id)
    if require_qualification:
        evidence_path = task_dir / "qualification" / "evidence.json"
        if not evidence_path.is_file() or evidence_path.is_symlink():
            raise ValueError("NSRR fixture qualification is missing")
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        if (evidence.get("task_id") != task_id or evidence.get("passed") is not True
                or evidence.get("fixed_input_fingerprint") != manifest["fixed_input_fingerprint"]
                or evidence.get("manifest_sha256") != _sha256(task_dir / "fixture_manifest.json")):
            raise ValueError("NSRR fixture qualification is stale or failed")
    runtime_dir = Path(runtime_dir).resolve()
    if not runtime_dir.is_relative_to(root / ".runtime"):
        raise ValueError("NSRR source checkouts must live under benchmark/.runtime")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    workspace = runtime_dir / task_id
    if workspace.exists():
        raise FileExistsError(f"NSRR checkout already exists; use a fresh runtime directory: {workspace}")
    copy_clean(source_path(root, manifest["code_dir"]), workspace)
    if (workspace / ".git").exists():
        raise ValueError("NSRR source checkout must not contain Git objects")
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_DATE": "2026-11-06T00:00:00+00:00", "GIT_COMMITTER_DATE": "2026-11-06T00:00:00+00:00"}
    for args in (["init", "--quiet"], ["add", "."],
                 ["-c", "user.name=NSRR Audit Fixture", "-c", "user.email=fixture@example.invalid",
                  "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "independent audit baseline"]):
        subprocess.run(["git", *args], cwd=workspace, env=env, check=True, capture_output=True, timeout=30)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, env=env, text=True).strip()
    if commit != manifest["commit"]:
        raise ValueError("materialized NSRR repo commit differs from the pinned manifest commit")
    protected = {}
    for target, relative in manifest["protected_acceptance_paths"].items():
        source = source_path(root, relative)
        if target in protected:
            raise ValueError("NSRR protected oracle path collision")
        protected[target] = str(source)
    task_file = source_path(root, manifest["task_file"])
    return {
        "task_id": task_id,
        "repository_id": manifest["repository_id"],
        "repo_path": str(workspace),
        "commit": commit,
        "prompt": manifest["prompt"],
        "acceptance_command": list(manifest["acceptance_command"]),
        "acceptance_files": list(manifest["acceptance_files"]),
        "protected_acceptance_paths": protected,
        "fixed_input_fingerprint": manifest["fixed_input_fingerprint"],
        "audit_use_only": True,
        "main_task_score_eligible": False,
    }


def _copy_hidden_acceptance(root: Path, manifest: dict, workspace: Path) -> None:
    tests = workspace / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    for target, relative in manifest["protected_acceptance_paths"].items():
        destination = workspace / target
        if destination.exists():
            raise ValueError(f"NSRR protected acceptance collision: {target}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path(root, relative), destination)


def _run_suite(root: Path, manifest: dict, source_key: str, suite: str, work: Path, python: str) -> dict:
    workspace = work / manifest["audit_task_id"] / source_key / suite
    copy_clean(source_path(root, manifest[source_key]), workspace)
    if suite in ("target", "combined"):
        _copy_hidden_acceptance(root, manifest, workspace)
    if suite == "public":
        argv = [python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_public.py", "-v"]
    elif suite == "target":
        argv = [python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_nsrr_acceptance.py", "-v"]
    else:
        argv = [python, *manifest["acceptance_command"][1:]]
    log = work / "logs" / f"{manifest['audit_task_id']}_{source_key}_{suite}.log"
    return execute(argv, workspace, log)


def qualify_nsrr_fixtures(root: Path, python: str = sys.executable) -> dict:
    """Run baseline/reference checks entirely offline and preserve command logs."""
    root = Path(root).resolve()
    evocode_source = root.parent / "src"
    if not evocode_source.is_dir():
        raise FileNotFoundError(f"EvoCode source tree needed for TaskSpec validation is missing: {evocode_source}")
    if str(evocode_source) not in sys.path:
        sys.path.insert(0, str(evocode_source))
    from evocode.contracts import TaskSpec

    public_spec_fields = {"repo_path", "prompt", "acceptance_command", "repository_id"}
    if not public_spec_fields <= set(TaskSpec.model_fields):
        raise ValueError("current EvoCode TaskSpec no longer satisfies the frozen NSRR adapter contract")
    work = root / ".runtime" / ("nsrr_fixture_qualification_" + uuid.uuid4().hex)
    records = []
    for task_id in TASK_IDS:
        manifest = load_nsrr_manifest(root, task_id)
        task_dir = _task_dir(root, task_id)
        variants: dict[str, dict] = {}
        for source_key in ("code_dir", "reference_dir"):
            variants[source_key] = {
                suite: _run_suite(root, manifest, source_key, suite, work, python)
                for suite in ("public", "target", "combined")
            }
        spec = materialize_nsrr_task_spec(root, task_id, work / "materialized_specs", require_qualification=False)
        public_task_spec = TaskSpec.model_validate({key: spec[key] for key in public_spec_fields})
        spec_valid = (
            public_task_spec.repository_id == manifest["repository_id"]
            and public_task_spec.acceptance_command == manifest["acceptance_command"]
            and Path(public_task_spec.repo_path).is_dir()
            and bool(re.fullmatch(r"[0-9a-f]{40}", spec.get("commit", "")))
            and set(spec.get("protected_acceptance_paths", {})) == set(manifest["protected_acceptance_paths"])
            and all(Path(value).is_file() and not Path(value).is_relative_to(Path(spec["repo_path"]))
                    for value in spec["protected_acceptance_paths"].values())
        )
        baseline, reference = variants["code_dir"], variants["reference_dir"]
        checks = {
            "sandbox_task_spec_validates": spec_valid,
            "baseline_public_passes": passed(baseline["public"]),
            "baseline_target_fails": completed(baseline["target"])
                and baseline["target"]["exit_code"] != 0
                and baseline["target"]["test_failures"] > 0
                and baseline["target"]["test_errors"] == 0,
            "reference_public_passes": passed(reference["public"]),
            "reference_target_passes": passed(reference["target"]),
            "reference_combined_passes": passed(reference["combined"]),
        }
        record = {
            "task_id": task_id,
            "repository_id": manifest["repository_id"],
            "fixed_input_fingerprint": manifest["fixed_input_fingerprint"],
            "manifest_sha256": _sha256(task_dir / "fixture_manifest.json"),
            "checks": checks,
            "passed": all(checks.values()),
            "commands": variants,
            "task_spec": {
                "public_fields": sorted(public_spec_fields),
                "repository_id": public_task_spec.repository_id,
                "commit": spec["commit"],
                "protected_acceptance_paths": sorted(spec["protected_acceptance_paths"]),
            },
            "model_calls": 0,
            "api_calls": 0,
            "formal_results": False,
            "main_task_score_eligible": False,
        }
        evidence_path = task_dir / "qualification" / "evidence.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_bytes(_canonical_json(record))
        records.append(record)
    return {
        "kind": "nsrr_independent_sandbox_fixture_qualification",
        "passed": len(records) == len(TASK_IDS) and all(row["passed"] for row in records),
        "task_count": len(records),
        "sandbox_task_specs_materialized": sum(row["checks"].get("sandbox_task_spec_validates") is True for row in records),
        "sandbox_task_spec_contract": sorted(public_spec_fields),
        "model_calls": 0,
        "api_calls": 0,
        "formal_results": False,
        "main_task_score_eligible": False,
        "work_dir": str(work),
        "tasks": records,
        "boundary": "Offline fixture executability and oracle separation only; candidate skills are not run, and no Agent, model, API, embedding, reranker, main T4, or main-task score is evaluated.",
    }
