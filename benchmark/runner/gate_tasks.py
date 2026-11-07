"""Trusted catalog and materialization for independent publication/replay tasks.

The host passes specs to Evolution.evaluate. Only code_dir is copied into each
source checkout; protected_acceptance_paths are consumed by SandboxEvaluator,
never mounted in the Coder workspace. Reference implementations are offline only.
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

STAGES = {"seed": 0, "transfer": 1, "stale": 2, "heldout": 3}
CATALOG = "configs/gate_task_catalog.json"
PATH_KEYS = ("code_dir", "task_file", "private_tests_dir", "regression_tests_dir", "reference_dir")


def _fingerprints(root: Path, manifest: dict) -> dict:
    values = {key: inventory(source_path(root, manifest[key])) for key in PATH_KEYS if key != "task_file"}
    values["task_file"] = hashlib.sha256(source_path(root, manifest["task_file"]).read_bytes()).hexdigest()
    return values


def load_gate_manifest(root: Path, task_id: str) -> dict:
    """Resolve an actual ID, verify frozen inputs and reject main-task sources."""
    root = Path(root).resolve()
    catalog = json.loads(source_path(root, CATALOG).read_text())
    manifest_path = source_path(root, catalog["tasks"][task_id])
    manifest = json.loads(manifest_path.read_text())
    repository_id = manifest.get("repository_id")
    if (manifest.get("task_id") != task_id or not isinstance(repository_id, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]*", repository_id)):
        raise ValueError("gate task identity mismatch")
    kind = manifest.get("kind")
    base = root / "private" / ("regression_replay" if kind == "regression" else "gate_selection") / repository_id
    if kind not in ("task", "meta", "regression") or not manifest_path.is_relative_to(base):
        raise ValueError("invalid independent task location or kind")
    stage = manifest.get("stage")
    if stage not in ("seed", "transfer", "stale"):
        raise ValueError("gate cannot depend on heldout material")
    if manifest.get("available_after_stage") != stage or manifest.get("latest_mechanism_stage") != stage:
        raise ValueError("gate stage provenance mismatch")
    if kind == "meta" and stage != "transfer":
        raise ValueError("Meta gate must be available at the T2 review boundary")
    if manifest.get("independent_of_main_tasks") is not True or manifest.get("future_main_inputs_used") != []:
        raise ValueError("gate provenance must explicitly exclude future main inputs")
    for key in PATH_KEYS:
        path = source_path(root, manifest[key])
        if not path.is_relative_to(manifest_path.parent):
            raise ValueError(f"independent task sources must remain within task directory: {key}")
    if manifest.get("acceptance_command") != ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]:
        raise ValueError("unsupported gate command")
    if manifest.get("fingerprints") != _fingerprints(root, manifest):
        raise ValueError("gate task inputs changed; re-freeze and requalify before use")
    return manifest


def require_gate_qualification(root: Path, task_id: str, manifest: dict) -> None:
    """Refuse an unqualified or changed fixture before any online dispatch."""
    catalog = json.loads(source_path(root, CATALOG).read_text())
    manifest_path = source_path(root, catalog["tasks"][task_id])
    evidence_path = manifest_path.parent / "qualification/evidence.json"
    if not evidence_path.is_file() or evidence_path.is_symlink():
        raise ValueError("independent task qualification is missing")
    evidence = json.loads(evidence_path.read_text())
    if (evidence.get("task_id") != task_id or evidence.get("passed") is not True
            or evidence.get("fingerprints") != manifest["fingerprints"]
            or evidence.get("manifest_sha256") != hashlib.sha256(manifest_path.read_bytes()).hexdigest()):
        raise ValueError("independent task qualification is stale or failed")


def _selected_ids(root: Path, stage: str, kind: str, repository_id: str | None = None) -> list[str]:
    catalog = json.loads(source_path(root, CATALOG).read_text())
    # Resolve IDs from metadata only. In particular the T2 Meta path never
    # opens/fingerprints a T3 task or any main-task material.
    selected = catalog["selection"][stage][kind]
    if not isinstance(selected, list) or len(set(selected)) != len(selected):
        raise ValueError("invalid independent task selection")
    for task_id in selected:
        available = catalog["availability"][task_id]
        if available["kind"] != kind or STAGES[available["stage"]] > STAGES[stage]:
            raise ValueError("independent task selection crosses stage/kind boundary")
        if kind != "regression" and available["stage"] != stage:
            raise ValueError("publication task stage mismatch")
    if repository_id is not None:
        # Repository scope comes from the trusted catalog, so selection does not
        # open/fingerprint unrelated repository manifests or task files.
        repositories = catalog.get("repository_ids", {})
        selected = [task_id for task_id in selected
                    if (repositories[task_id] if task_id in repositories
                        else load_gate_manifest(root, task_id).get("repository_id")) == repository_id]
    return selected


def materialize_gate_spec(root: Path, task_id: str, runtime_dir: Path, *, stage: str) -> dict:
    """Create one oracle-free single-commit baseline, returning a trusted spec.

    The caller must keep this spec host-side: protected_acceptance_paths contain
    oracle locations. Application.run_isolated reduces it to the public TaskSpec.
    """
    root = Path(root).resolve()
    catalog = json.loads(source_path(root, CATALOG).read_text())
    available = catalog["availability"][task_id]
    if stage not in STAGES or stage == "heldout" or STAGES[available["stage"]] > STAGES[stage]:
        raise ValueError("gate task is not available at this stage boundary")
    manifest = load_gate_manifest(root, task_id)
    declared_repository = catalog.get("repository_ids", {}).get(task_id)
    if declared_repository is not None and declared_repository != manifest["repository_id"]:
        raise ValueError("catalog/manifest repository scope mismatch")
    if available != {"stage": manifest["stage"], "kind": manifest["kind"]}:
        raise ValueError("catalog/manifest provenance mismatch")
    if manifest["kind"] != "regression" and manifest["stage"] != stage:
        raise ValueError("publication gate must match the current stage")
    require_gate_qualification(root, task_id, manifest)
    runtime_dir = Path(runtime_dir).resolve()
    if not runtime_dir.is_relative_to(root / ".runtime"):
        raise ValueError("gate source checkouts must live under benchmark/.runtime")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    workspace = runtime_dir / (task_id + "_" + uuid.uuid4().hex)
    copy_clean(source_path(root, manifest["code_dir"]), workspace)
    if (workspace / ".git").exists():
        raise ValueError("gate code baseline must not contain Git objects")
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_DATE": "2026-11-06T00:00:00+00:00", "GIT_COMMITTER_DATE": "2026-11-06T00:00:00+00:00"}
    for args in (["init", "--quiet"], ["add", "."],
                 ["-c", "user.name=Benchmark Fixture", "-c", "user.email=fixture@example.invalid",
                  "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "independent gate baseline"]):
        subprocess.run(["git", *args], cwd=workspace, env=env, check=True, capture_output=True, timeout=30)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, env=env, text=True).strip()
    protected = {}
    for directory in ("code_dir", "private_tests_dir", "regression_tests_dir"):
        source = source_path(root, manifest[directory])
        for relative in inventory(source):
            path = Path(relative)
            if directory == "code_dir":
                if path.parts[0] != "tests": continue
                target = relative
            else:
                target = (Path("tests") / relative).as_posix()
            if target in protected:
                raise ValueError("independent oracle filename collision")
            protected[target] = str(source / relative)
    return {"task_id": task_id, "repository_id": manifest["repository_id"], "stage": manifest["stage"],
            "gate_kind": manifest["kind"],
            "repo_path": str(workspace), "commit": commit,
            "prompt": source_path(root, manifest["task_file"]).read_text(),
            "acceptance_command": manifest["acceptance_command"],
            "protected_acceptance_paths": protected}


def build_gate_specs(root: Path, stage: str, kind: str, runtime_dir: Path,
                     repository_id: str | None = "repo_api") -> list[dict]:
    """Load stage Task/Meta gates or fixed replay tasks for Evolution.evaluate.

    kind='task': the current stage's independent project task;
    kind='meta': the separate transfer-only downstream Meta task;
    kind='regression': fixed replay tasks available since seed.
    Heldout never invokes evolution; no specs are produced for it.
    """
    if stage not in STAGES or stage == "heldout" or kind not in ("task", "meta", "regression"):
        raise ValueError("unsupported gate stage or kind")
    if kind == "meta" and stage != "transfer":
        raise ValueError("Meta evaluation is only allowed after transfer")
    root = Path(root).resolve()
    ids = _selected_ids(root, stage, kind, repository_id)
    if not ids:
        raise ValueError("no independent task configured for stage/kind")
    return [materialize_gate_spec(root, task_id, runtime_dir, stage=stage) for task_id in ids]


def validate_gate_tasks(root: Path, python: str = sys.executable,
                        repository_id: str | None = None,
                        *, persist_evidence: bool = True) -> dict:
    """Run baseline/reference admission without models; preserve actual logs."""
    root = Path(root).resolve()
    catalog = json.loads(source_path(root, CATALOG).read_text())
    run_dir = root / ".runtime" / ("gate_qualification_" + uuid.uuid4().hex)
    records = []
    for task_id in catalog["tasks"]:
        manifest = load_gate_manifest(root, task_id)
        if repository_id is not None and manifest["repository_id"] != repository_id:
            continue
        log_dir = source_path(root, str(Path(catalog["tasks"][task_id]).parent)) / "qualification"
        commands = {}
        for variant, source_key in (("baseline", "code_dir"), ("reference", "reference_dir")):
            suites = {}
            for suite in ("public", "target", "regression", "combined"):
                workspace = run_dir / task_id / variant / suite
                copy_clean(source_path(root, manifest[source_key]), workspace)
                if suite in ("target", "regression", "combined"):
                    for key in (("private_tests_dir", "regression_tests_dir") if suite == "combined" else
                                (("private_tests_dir",) if suite == "target" else ("regression_tests_dir",))):
                        source = source_path(root, manifest[key])
                        for relative in inventory(source):
                            destination = workspace / "tests" / relative
                            if destination.exists(): raise ValueError("oracle collision")
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(source / relative, destination)
                log_path = log_dir / f"{variant}_{suite}.log"
                raw = execute([python, *manifest["acceptance_command"][1:]], workspace, log_path)
                raw["argv"] = ["python", *manifest["acceptance_command"][1:]]
                raw["cwd"] = workspace.relative_to(root).as_posix()
                raw["log"] = log_path.relative_to(root).as_posix()
                suites[suite] = raw
            commands[variant] = suites
        baseline, reference = commands["baseline"], commands["reference"]
        checks = {"baseline_public_passes": passed(baseline["public"]),
                  "baseline_target_fails": completed(baseline["target"]) and baseline["target"]["exit_code"] != 0
                                           and baseline["target"]["test_failures"] > 0 and baseline["target"]["test_errors"] == 0,
                  "baseline_regression_passes": passed(baseline["regression"]),
                  "reference_all_passes": all(passed(result) for result in reference.values())}
        record = {"task_id": task_id, "manifest": catalog["tasks"][task_id], "kind": manifest["kind"],
                  "stage": manifest["stage"], "checks": checks, "passed": all(checks.values()),
                  "manifest_sha256": hashlib.sha256(source_path(root, catalog["tasks"][task_id]).read_bytes()).hexdigest(),
                  "fingerprints": manifest["fingerprints"], "commands": commands,
                  "model_calls": 0, "formal_results": False}
        if persist_evidence:
            (log_dir / "evidence.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        records.append(record)
    return {"kind": "independent_gate_and_replay_offline_admission", "repository_id": repository_id,
            "passed": bool(records) and all(row["passed"] for row in records),
            "model_calls": 0, "formal_results": False, "tasks": records,
            "boundary": "Only fixture executability and oracle isolation are checked; no model, skill benefit or formal benchmark result."}
