"""Protocol-driven three-repository EvoCode formal benchmark runner.

The module composes the existing chain journal, Agent adapter, stage snapshots,
complete artifact collection and independent grader. Model execution is only
possible through the explicitly constructed LiveBackend in run_formal.py.
"""
from __future__ import annotations

import asyncio
import copy
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import random
import re
import time
from pathlib import Path
from typing import Any, Protocol

from runner.chain import ChainRunner, BudgetStopped, atomic_json, digest, read_json, relative_paths
from runner.dataset_validation import STAGES, inventory, load_manifest, source_path
from runner.stage_packages import validate_stage_package

REPOSITORIES = ("repo_api", "repo_cli", "repo_data")
CONDITIONS_A = ("A1", "A2")
CONDITIONS_B = ("B2", "B3", "B4")
NON_POSITIVE_LABELS = {"harmful", "neutral"}
AUDIT_TYPES = {"duplicate", "stale", "potentially_harmful", "potentially_beneficial"}
AUDIT_LABELS = {"harmful", "neutral", "beneficial", "unknown"}


def load_protocol(root: Path) -> dict:
    import yaml
    value = yaml.safe_load((root / "configs/protocol.yaml").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("protocol must be a mapping")
    return value


def formal_execution_schedule(root: Path, protocol: dict | None = None) -> list[dict]:
    """Return the frozen 54-position matrix with seeded, recorded A/B order."""
    protocol = protocol or load_protocol(root)
    dataset = protocol["dataset"]
    profile = protocol["profiles"]["formal_benchmark"]
    repositories = list(profile["repositories"])
    stage_names = list(dataset["stage_order"])
    stage_directories = dataset["stage_directories"]
    seed = profile["seeds"][0]
    rng = random.Random(seed)
    rows: list[dict] = []
    position = 0

    for stage_name in protocol["experiment_a"]["scored_stages"]:
        order = list(CONDITIONS_A)
        rng.shuffle(order)
        arm_order = {condition: index + 1 for index, condition in enumerate(order)}
        for condition in order:
            for repository_id in repositories:
                position += 1
                stage = stage_directories[stage_name]
                manifest = load_manifest(root, stage, repository_id)
                rows.append({"position": position, "experiment": "A", "condition": condition,
                    "repository_id": repository_id, "stage": stage, "stage_name": stage_name,
                    "task_id": manifest["task_id"], "attempt_id": f"{condition}_{repository_id}_{stage_name}",
                    "repeat_id": 1, "seed": seed,
                    "wave_id": f"A_{stage_name}",
                    "pair_id": f"A_{stage_name}_{repository_id}",
                    "pair_order": arm_order[condition], "pair_arm_order": order})

    b_order = list(CONDITIONS_B)
    rng.shuffle(b_order)
    for condition in b_order:
        for stage_name in stage_names:
            for repository_id in repositories:
                position += 1
                stage = stage_directories[stage_name]
                manifest = load_manifest(root, stage, repository_id)
                rows.append({"position": position, "experiment": "B", "condition": condition,
                    "repository_id": repository_id, "stage": stage, "stage_name": stage_name,
                    "task_id": manifest["task_id"], "attempt_id": f"{condition}_{repository_id}_{stage_name}",
                    "repeat_id": 1, "seed": seed,
                    "wave_id": f"B_{condition}_{stage_name}", "pair_id": None,
                    "pair_order": None, "pair_arm_order": None})

    attempts = [row["attempt_id"] for row in rows]
    if len(rows) != 54 or len(set(attempts)) != 54:
        raise ValueError("formal planned matrix must contain exactly 54 unique positions")
    counts = {condition: sum(row["condition"] == condition for row in rows)
              for condition in (*CONDITIONS_A, *CONDITIONS_B)}
    if counts != {"A1": 9, "A2": 9, "B2": 12, "B3": 12, "B4": 12}:
        raise ValueError(f"formal matrix condition counts invalid: {counts}")
    if any(row["stage_name"] == "seed" for row in rows if row["experiment"] == "A"):
        raise ValueError("formal A matrix must not contain T1 positions")
    return rows


def validate_formal_structure(protocol: dict) -> list[str]:
    """Check frozen counts, scopes and boundaries without touching services."""
    errors: list[str] = []
    dataset = protocol.get("dataset", {})
    profile = protocol.get("profiles", {}).get("formal_benchmark", {})
    if profile.get("formal_results") is not True:
        errors.append("formal_benchmark must declare formal_results=true")
    if profile.get("repositories") != list(REPOSITORIES):
        errors.append("formal_benchmark repository order must be repo_api, repo_cli, repo_data")
    if profile.get("repeats") != 1 or profile.get("seeds") != [42]:
        errors.append("formal_benchmark must use one repeat and seed 42")
    if profile.get("main_runs_per_repeat") != 54 or profile.get("total_main_runs") != 54:
        errors.append("formal_benchmark must declare exactly 54 main positions")
    if dataset.get("stage_order") != ["seed", "transfer", "stale", "heldout"]:
        errors.append("dataset stage order differs from frozen protocol")
    if protocol.get("experiment_a", {}).get("scored_stages") != ["transfer", "stale", "heldout"]:
        errors.append("A must score T2/T3/T4 only")
    if profile.get("meta_review_repositories") != list(REPOSITORIES):
        errors.append("formal B4 Meta barrier must include all three repositories")
    if profile.get("nsrr", {}).get("enabled") is not True or profile.get("nsrr", {}).get("candidate_count") != 8:
        errors.append("formal NSRR must be enabled for exactly eight audit candidates")
    execution = protocol.get("execution", {})
    if execution.get("dispatch_policy") != "phase_barrier":
        errors.append("formal execution must declare dispatch_policy=phase_barrier")
    if "sequential_inference" in execution:
        errors.append("sequential_inference conflicts with the formal phase-barrier schedule")
    expected_parallelism = {
        "policy": "phase_barrier",
        "b_tasks_per_stage": 3, "a_pairs_per_stage": 3, "a_arms_per_pair": 2,
        "task_learning_proposals_per_stage": 3, "task_gate_evaluations_per_stage": 3,
        "t4_tasks_per_condition": 3, "nsrr_candidates": 1,
        "result_merge_order": "repository_order",
        "meta_task_trace_order": "task_created_at_then_repository_order_then_task_id",
        "agent_grading": "after_all_wave_tasks_terminal",
        "task_state_mode": "shared_application_unique_workspaces",
        "sqlite_databases_per_experiment_group": 1,
        "sqlite_isolation_across_conditions": "independent_cloned_state",
        "repository_scope_within_group": "repository_id",
        "skill_publication": "after_all_stage_task_gates_terminal_in_repository_order",
    }
    if profile.get("parallelism") != expected_parallelism:
        errors.append("formal parallelism matrix differs from the frozen phase-barrier contract")
    if protocol.get("profiles", {}).get("chain_integration", {}).get("parallelism") != {
            "policy": "serial_chain_integration", "main_tasks_per_stage": 1}:
        errors.append("chain_integration must remain serial with one task per stage")
    if execution.get("sqlite_concurrency") != {"journal_mode": "WAL",
            "connection_timeout_seconds": 30, "busy_timeout_milliseconds": 30000,
            "write_serialization": "eventstore_lock_begin_immediate"}:
        errors.append("SQLite concurrency settings differ from the frozen formal contract")
    if protocol.get("revision") != 2 or protocol.get("protocol_id") != "evocode-benchmark-2026-11-06-r2":
        errors.append("formal phase-barrier protocol must use revision 2")
    if execution.get("seed_unsupported_behavior") != "record_unsupported":
        errors.append("unsupported model seeds must be recorded")
    if protocol.get("experiment_b", {}).get("meta_review_scope") != "after_all_profile_repositories":
        errors.append("B4 Meta review must wait for all profile repositories")
    if protocol.get("experiment_b", {}).get("heldout_clone") != "independent_per_task_from_frozen_group_state":
        errors.append("T4 must independently clone each frozen group snapshot")
    if set(dataset.get("stage_directories", {})) != {"seed", "transfer", "stale", "heldout"}:
        errors.append("dataset stage directory map is incomplete")
    return errors


def _catalog_ids(root: Path, stage: str, kind: str, repository_id: str | None = None) -> list[str]:
    from runner.gate_tasks import _selected_ids
    return _selected_ids(root, stage, kind, repository_id)


def validate_gate_regression_matrix(root: Path, protocol: dict) -> dict:
    """Validate declared Gate/replay IDs against catalog, manifest and scope."""
    from runner.gate_tasks import load_gate_manifest, require_gate_qualification
    root = root.resolve()
    profile = protocol["profiles"]["formal_benchmark"]
    repositories = profile["repositories"]
    errors: list[str] = []
    checked: dict[str, Any] = {"task_gates": {}, "meta_gates": [], "regression": {}}
    catalog_value = profile.get("task_catalog")
    if catalog_value != "configs/gate_task_catalog.json":
        errors.append("formal task_catalog must point to configs/gate_task_catalog.json")
    try:
        catalog_path = source_path(root, str(catalog_value))
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog_fingerprint = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    except Exception as exc:
        return {"valid": False, "errors": [f"Gate catalog unreadable: {type(exc).__name__}"], "matrix": checked}

    for stage in ("seed", "transfer", "stale"):
        declared = profile.get("gate_task_ids", {}).get(stage)
        if not isinstance(declared, dict):
            errors.append(f"{stage}: Gate matrix missing")
            continue
        general = declared.get("general")
        if general != []:
            errors.append(f"{stage}: nonempty General scope is unsupported until its matrix is declared")
        project = declared.get("project")
        if not isinstance(project, dict) or set(project) != set(repositories):
            errors.append(f"{stage}: project Gate repository scopes mismatch")
            continue
        checked["task_gates"][stage] = {}
        for repository_id in repositories:
            selected = project.get(repository_id)
            actual = _catalog_ids(root, stage, "task", repository_id)
            if selected != actual or not isinstance(selected, list) or not selected:
                errors.append(f"{stage}/{repository_id}: profile and independent Task Gate IDs differ")
            for task_id in selected if isinstance(selected, list) else []:
                try:
                    manifest = load_gate_manifest(root, task_id)
                    require_gate_qualification(root, task_id, manifest)
                    if manifest["kind"] != "task" or manifest["stage"] != stage or manifest["repository_id"] != repository_id:
                        raise ValueError("Gate task kind/stage/repository mismatch")
                except Exception as exc:
                    errors.append(f"{stage}/{repository_id}/{task_id}: {type(exc).__name__}: {exc}")
            checked["task_gates"][stage][repository_id] = list(selected or [])
        declared_meta = declared.get("meta", [])
        actual_meta = _catalog_ids(root, stage, "meta", None)
        if stage == "transfer":
            if declared_meta != actual_meta or not declared_meta:
                errors.append("transfer: profile and independent Meta Gate IDs differ")
            for task_id in declared_meta if isinstance(declared_meta, list) else []:
                try:
                    manifest = load_gate_manifest(root, task_id)
                    require_gate_qualification(root, task_id, manifest)
                    if manifest["kind"] != "meta" or manifest["stage"] != "transfer":
                        raise ValueError("Meta Gate kind/stage mismatch")
                except Exception as exc:
                    errors.append(f"transfer/meta/{task_id}: {type(exc).__name__}: {exc}")
            checked["meta_gates"] = list(declared_meta or [])
        elif declared_meta != []:
            errors.append(f"{stage}: Meta Gates are only allowed at transfer")

    regression_profile = profile.get("regression_task_ids", {})
    if not isinstance(regression_profile, dict) or set(regression_profile) != set(repositories):
        errors.append("fixed regression repository scopes mismatch")
    for repository_id in repositories:
        declared = regression_profile.get(repository_id) if isinstance(regression_profile, dict) else None
        actual = _catalog_ids(root, "seed", "regression", repository_id)
        if declared != actual or not isinstance(declared, list) or not declared:
            errors.append(f"{repository_id}: profile and fixed regression IDs differ")
        for task_id in declared if isinstance(declared, list) else []:
            try:
                manifest = load_gate_manifest(root, task_id)
                require_gate_qualification(root, task_id, manifest)
                if manifest["kind"] != "regression" or manifest["repository_id"] != repository_id:
                    raise ValueError("regression repository scope mismatch")
            except Exception as exc:
                errors.append(f"regression/{repository_id}/{task_id}: {type(exc).__name__}: {exc}")
        checked["regression"][repository_id] = list(declared or [])

    checked["catalog_sha256"] = catalog_fingerprint
    checked["catalog_task_count"] = len(catalog.get("tasks", {}))
    return {"valid": not errors, "errors": errors, "matrix": checked}


class NSRRAuditReader(Protocol):
    def read(self, root: Path, audit_task_ids: list[str]) -> list[dict]: ...


class NSRRGateEvaluator(Protocol):
    """Adapter for the direct external-candidate Gate API.

    Submit POST /api/evolution/external-candidates/evaluate with
    {batch_id, frozen_memory_collection, audit_fixture_root, candidates:[{
    candidate_id, repository_scope, baseline_skill, candidate_skill, task_specs}]}.
    Poll GET /api/evolution/external-candidates/{batch_id} until terminal. The
    Runner materializes each frozen TaskSpec under the private fixture root and
    injects the audit/fixture trust roots only into that isolated Agent process.
    A completed record must preserve the API source, candidate/run/evaluation
    IDs, actual boolean evaluation result, decision and persisted report. Failed
    runs remain explicit terminal records and cannot enter K/N. No endpoint may
    publish, rollback, write Raw-RAG or expose these records to later learning.

    The Gate receives only frozen baseline/candidate SKILL.md text. Candidate
    policy.json participates only in independent offline label generation; it
    is neither appended to the Skill prompt nor sent to the direct evaluator.
    The API must keep each audit run out of the main task/Meta learning trace.
    """
    async def evaluate(self, root: Path, batch_id: str, audit_rows: list[dict]) -> dict: ...


class FrozenNSRRAuditReader:
    """Read independently observed NSRR labels and validate the frozen lock.

    This reader never invokes a model, Evolution, Raw-RAG, Gate or Grader.
    """

    def read(self, root: Path, audit_task_ids: list[str]) -> list[dict]:
        root = root.resolve()
        if len(audit_task_ids) != 8 or len(set(audit_task_ids)) != 8:
            raise ValueError("NSRR requires eight unique protocol audit_task_ids")
        audit_root = root / "private/gate_audit"
        lock_path = audit_root / "freeze_manifest.json"
        labels_path = audit_root / "observed_labels.json"
        lock = read_json(lock_path)
        labels_file = read_json(labels_path)
        lock_hash = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        if labels_file.get("freeze_manifest_sha256") != lock_hash:
            raise ValueError("NSRR observed labels do not bind the frozen lock")
        if labels_file.get("audit_version") != lock.get("audit_version") or labels_file.get("rubric_id") != lock.get("rubric_id"):
            raise ValueError("NSRR audit version/rubric mismatch")
        runner_hash = lock.get("runner_sha256")
        if not isinstance(runner_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", runner_hash):
            raise ValueError("NSRR runner hash is missing or invalid")
        inputs = lock.get("inputs")
        if not isinstance(inputs, dict) or not inputs:
            raise ValueError("NSRR frozen input manifest is missing")
        for relative, expected in inputs.items():
            path = source_path(root, relative)
            if not isinstance(expected, str) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"NSRR frozen input changed: {relative}")
        if runner_hash not in set(inputs.values()):
            raise ValueError("NSRR runner hash is not bound by the frozen input manifest")

        candidates = []
        for audit_task_id in audit_task_ids:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", audit_task_id):
                raise ValueError("unsafe NSRR audit task id")
            matches = []
            for path in sorted((root / "gate_cases").glob("*/candidate.json")):
                candidate = read_json(path)
                if candidate.get("audit_task_id") == audit_task_id:
                    matches.append((path, candidate))
            if len(matches) != 1:
                raise ValueError(f"NSRR task ID must map to exactly one frozen candidate: {audit_task_id}")
            path, candidate = matches[0]
            candidate_id = candidate.get("candidate_id")
            candidate_type = candidate.get("candidate_type")
            if not isinstance(candidate_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", candidate_id):
                raise ValueError("NSRR candidate identity invalid")
            if candidate_type not in AUDIT_TYPES or candidate.get("audit_task_id") != audit_task_id:
                raise ValueError("NSRR candidate type/task identity invalid")
            expected_candidate_path = path.relative_to(root).as_posix()
            if expected_candidate_path not in inputs:
                raise ValueError("NSRR candidate manifest is not covered by freeze lock")
            for field in ("baseline_skill", "candidate_skill", "baseline_policy", "candidate_policy"):
                value = candidate.get(field)
                if not isinstance(value, str):
                    raise ValueError(f"NSRR candidate missing {field}")
                source_path(root, value)
            if not isinstance(candidate.get("source_provenance"), str) or not candidate["source_provenance"].strip():
                raise ValueError("NSRR candidate source_provenance is missing")
            task_root = root / "private/gate_audit/tasks" / audit_task_id
            for name in ("task.json", "trusted_tests.json", "fixture_manifest.json"):
                relative = (task_root / name).relative_to(root).as_posix()
                if relative not in inputs:
                    raise ValueError(f"NSRR audit input not covered by freeze lock: {relative}")
                source_path(root, relative)
            candidates.append({"candidate": candidate, "candidate_manifest": expected_candidate_path})

        from runner.nsrr_audit_tasks import load_nsrr_manifest, nsrr_fixture_input_paths
        fixture_inputs = nsrr_fixture_input_paths(root)
        if not set(fixture_inputs) <= set(inputs):
            missing = sorted(set(fixture_inputs) - set(inputs))
            raise ValueError("NSRR sandbox fixture inputs are not covered by freeze lock: " + ", ".join(missing))
        fixture_manifests = {task_id: load_nsrr_manifest(root, task_id) for task_id in audit_task_ids}

        observed = labels_file.get("observed_labels")
        if not isinstance(observed, list) or len(observed) != 8:
            raise ValueError("NSRR must contain eight independent observed labels")
        by_task = {}
        for row in observed:
            task_id = row.get("audit_task_id")
            if task_id in by_task:
                raise ValueError("duplicate NSRR observed task label")
            if row.get("observed_label") not in AUDIT_LABELS:
                raise ValueError("unsupported NSRR external label")
            by_task[task_id] = row
        if set(by_task) != set(audit_task_ids):
            raise ValueError("NSRR observed labels differ from profile audit_task_ids")

        # Verify that the locked, deterministic offline audit reproduces the
        # persisted labels. This path reads only gate_cases/ and private/gate_audit/.
        runner_path = root / "scripts/run_nsrr_audit.py"
        spec = importlib.util.spec_from_file_location("benchmark_frozen_nsrr_audit", runner_path)
        if spec is None or spec.loader is None:
            raise ValueError("frozen NSRR audit runner cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _report, reproduced_labels = module.run_audit()
        if reproduced_labels != labels_file:
            raise ValueError("persisted NSRR observations do not match frozen deterministic audit")

        result = []
        seen_candidates = set()
        for item in candidates:
            candidate = item["candidate"]
            external = by_task[candidate["audit_task_id"]]
            if external.get("candidate_id") != candidate["candidate_id"] or external.get("candidate_type") != candidate["candidate_type"]:
                raise ValueError("NSRR observed label identity/type mismatch")
            if candidate["candidate_id"] in seen_candidates:
                raise ValueError("duplicate NSRR candidate identity")
            seen_candidates.add(candidate["candidate_id"])
            result.append({"candidate_id": candidate["candidate_id"],
                "candidate_type": candidate["candidate_type"], "audit_task_id": candidate["audit_task_id"],
                "observed_label": external["observed_label"],
                "baseline_observation": external.get("baseline_observation"),
                "candidate_observation": external.get("candidate_observation"),
                "rule_id": external.get("rule_id"), "evidence_refs": external.get("evidence_refs", []),
                "version_hashes": external.get("version_hashes", {}),
                "candidate_manifest": item["candidate_manifest"], "label_source": "private/gate_audit/observed_labels.json",
                "freeze_manifest_sha256": lock_hash,
                "fixture_manifest": f"private/gate_audit/tasks/{candidate['audit_task_id']}/fixture_manifest.json",
                "fixture_manifest_sha256": hashlib.sha256((root / "private/gate_audit/tasks" / candidate["audit_task_id"] / "fixture_manifest.json").read_bytes()).hexdigest(),
                "fixed_input_fingerprint": fixture_manifests[candidate["audit_task_id"]]["fixed_input_fingerprint"]})
        return result


def validate_nsrr_gate_decisions(records: list[dict], batch_id: str | None = None) -> list[dict]:
    """Accept only explicit records produced by the independent NSRR Gate path."""
    checked = []
    seen_candidates = set()
    for row in records:
        if not isinstance(row.get("candidate_id"), str) or not row["candidate_id"]:
            raise ValueError("NSRR evaluation record must identify a candidate")
        if row.get("status") not in {"queued", "running", "evaluating", "evaluated", "needs_review", "failed", "interrupted"}:
            raise ValueError("NSRR evaluation record has an unsupported run status")
        if row["candidate_id"] in seen_candidates:
            raise ValueError("NSRR batch returned more than one record for a candidate")
        seen_candidates.add(row["candidate_id"])
        if batch_id is not None and row.get("batch_id", batch_id) != batch_id:
            raise ValueError("NSRR Gate record belongs to a different batch")
        # Preserve actual failed/pending runs as missing decisions. They cannot
        # contribute to K/N, but their status remains in the NSRR output.
        if row.get("decision") is None and row.get("evaluation_passed") is None:
            checked.append({**row, "batch_id": batch_id or row.get("batch_id")})
            continue
        if row.get("source") != "nsrr_candidate_evaluation_api_record":
            raise ValueError("NSRR Gate decisions must come from the independent candidate-evaluation API")
        if row.get("decision") not in {"accepted", "blocked"}:
            raise ValueError("NSRR Gate decision must be accepted or blocked")
        if not row.get("candidate_id") or not row.get("run_id"):
            raise ValueError("NSRR Gate decision must identify candidate and actual run")
        if not row.get("evaluation_id"):
            raise ValueError("NSRR Gate decision must identify a durable evaluation record")
        if type(row.get("evaluation_passed")) is not bool:
            raise ValueError("NSRR Gate decision requires the actual boolean evaluation result")
        if not isinstance(row.get("evaluation"), dict) or row["evaluation"].get("passed") is not row["evaluation_passed"]:
            raise ValueError("NSRR Gate record must contain the matching persisted evaluation report")
        expected = "accepted" if row["evaluation_passed"] else "blocked"
        if row["decision"] != expected:
            raise ValueError("NSRR Gate decision contradicts its actual evaluation record")
        checked.append(row)
    return checked


def summarize_nsrr(audit_rows: list[dict], gate_decisions: list[dict]) -> dict:
    """Compute K/N only when actual gate decisions join to independent labels."""
    ids = [row.get("candidate_id") for row in audit_rows]
    if len(ids) != 8 or len(set(ids)) != 8:
        return {"status": "unverified", "value": None, "reason": "NSRR audit matrix is not eight unique candidates"}
    decisions_by_id: dict[str, list[dict]] = {}
    record_ids = set()
    for decision in gate_decisions:
        record_ids.add(decision.get("candidate_id"))
        if decision.get("source") != "nsrr_candidate_evaluation_api_record":
            continue
        if decision.get("decision") not in {"accepted", "blocked"}:
            continue
        decisions_by_id.setdefault(decision.get("candidate_id"), []).append(decision)
    if any(len(decisions_by_id.get(candidate_id, [])) > 1 for candidate_id in ids):
        return {"status": "unverified", "value": None, "reason": "multiple actual Gate records map to one NSRR candidate"}

    unexpected = sorted(record_id for record_id in record_ids if record_id not in set(ids))
    if unexpected:
        return {"status": "unverified", "value": None,
            "reason": "Gate API returned candidate IDs outside the frozen NSRR matrix",
            "unexpected_gate_candidates": unexpected}

    adverse = [row for row in audit_rows if row.get("observed_label") in NON_POSITIVE_LABELS]
    unknown = [row for row in audit_rows if row.get("observed_label") == "unknown"]
    beneficial = [row for row in audit_rows if row.get("observed_label") == "beneficial"]
    known_adverse = [row for row in adverse if decisions_by_id.get(row["candidate_id"])]
    blocked = sum(decisions_by_id[row["candidate_id"]][0]["decision"] == "blocked" for row in known_adverse)
    beneficial_with_decisions = [row for row in beneficial if decisions_by_id.get(row["candidate_id"])]
    accepted_beneficial = sum(decisions_by_id[row["candidate_id"]][0]["decision"] == "accepted"
                              for row in beneficial_with_decisions)
    missing_adverse = [row["candidate_id"] for row in adverse if not decisions_by_id.get(row["candidate_id"])]
    missing_beneficial = [row["candidate_id"] for row in beneficial if not decisions_by_id.get(row["candidate_id"])]
    if not adverse:
        status, value = "not_applicable", None
    elif not missing_adverse:
        status, value = "calculated", blocked / len(adverse)
    else:
        status, value = "unverified", None
    completed_ids = set(decisions_by_id)
    terminal_statuses = {"evaluated", "needs_review", "failed", "interrupted"}
    terminal_ids = {decision.get("candidate_id") for decision in gate_decisions
                    if decision.get("status") in terminal_statuses}
    missing_decisions_all = sorted(candidate_id for candidate_id in ids if candidate_id not in completed_ids)
    missing_terminal_positions = sorted(candidate_id for candidate_id in ids if candidate_id not in terminal_ids)
    return {"status": status, "formula": "K/N", "value": value, "K": blocked if not missing_adverse else None,
        "N": len(adverse), "K_observed": blocked, "N_labeled": len(adverse),
        "known_gate_decisions_for_adverse": len(known_adverse), "missing_gate_decisions": missing_adverse,
        "adverse_decisions_complete": not missing_adverse,
        "beneficial_decisions_complete": not missing_beneficial,
        "matrix_status": "complete" if not missing_terminal_positions else ("partial" if terminal_ids else "unverified"),
        "terminal_candidate_count": len(terminal_ids), "planned_candidate_count": len(ids),
        "completed_decision_count": len(completed_ids),
        "missing_gate_decision_candidates": missing_decisions_all,
        "missing_terminal_positions": missing_terminal_positions,
        "unknown_label_count": len(unknown), "beneficial_candidate_count": len(beneficial),
        "missing_beneficial_gate_decisions": missing_beneficial,
        "beneficial_gate_accept_count": accepted_beneficial if not missing_beneficial else None,
        "beneficial_gate_decisions_observed": len(beneficial_with_decisions),
        "candidate_types_are_labels": False,
        "external_label_source": "private/gate_audit/observed_labels.json",
        "gate_decision_source": "independent NSRR candidate-evaluation API record"}


class FormalRunner(ChainRunner):
    """Formal single-repeat runner for the frozen 54-position matrix."""

    def __init__(self, root: Path, batch_id: str, backend, *, protocol: dict | None = None,
                 gate_provider=None, grader=None, batch_metadata: dict | None = None,
        nsrr_reader: NSRRAuditReader | None = None, nsrr_gate_evaluator: NSRRGateEvaluator | None = None,
        require_ready: bool | None = None):
        self.protocol = protocol or load_protocol(root)
        errors = validate_formal_structure(self.protocol)
        if errors:
            raise ValueError("invalid formal protocol: " + "; ".join(errors))
        repositories = self.protocol["profiles"]["formal_benchmark"]["repositories"]
        self.repositories = list(repositories)
        self.dataset = self.protocol["dataset"]
        self.profile = self.protocol["profiles"]["formal_benchmark"]
        self.stage_names = list(self.dataset["stage_order"])
        self.stage_directories = self.dataset["stage_directories"]
        self.nsrr_reader = nsrr_reader or FrozenNSRRAuditReader()
        self.nsrr_gate_evaluator = nsrr_gate_evaluator
        super().__init__(root, batch_id, backend, gate_provider=gate_provider, grader=grader,
            batch_metadata=batch_metadata, repository_id=self.repositories[0], formal_results=True)
        self.schedule = formal_execution_schedule(self.root, self.protocol)
        self.schedule_by_attempt = {row["attempt_id"]: row for row in self.schedule}
        self.require_ready = (not self.offline) if require_ready is None else require_ready
        self.gate_matrix = validate_gate_regression_matrix(self.root, self.protocol)
        if not self.gate_matrix["valid"]:
            raise ValueError("invalid formal Gate/regression matrix: " + "; ".join(self.gate_matrix["errors"]))
        self.manifests = {repository_id: {
            self.stage_directories[name]: load_manifest(self.root, self.stage_directories[name], repository_id)
            for name in self.stage_names} for repository_id in self.repositories}
        self.dataset_evidence = self._validate_datasets()
        self.nsrr_rows = None
        try:
            audit_ids = self.profile.get("nsrr", {}).get("audit_task_ids")
            self.nsrr_rows = self.nsrr_reader.read(self.root, audit_ids)
        except Exception as exc:
            self.nsrr_error = f"{type(exc).__name__}: {exc}"
        else:
            self.nsrr_error = None
        self.nsrr_decisions: list[dict] = []
        self._seed_supported = False

    def _validate_datasets(self) -> dict:
        manifests = {}
        for repository_id in self.repositories:
            manifests[repository_id] = {}
            for stage_name in self.stage_names:
                stage = self.stage_directories[stage_name]
                manifest = load_manifest(self.root, stage, repository_id)
                if manifest.get("repository_id") != repository_id or manifest.get("stage") != stage_name:
                    raise ValueError(f"dataset manifest scope mismatch: {repository_id}/{stage_name}")
                package = validate_stage_package(self.root, manifest)
                manifests[repository_id][stage_name] = {"manifest": manifest, "stage_package": package}
        return manifests

    def _input_fingerprint(self) -> dict:
        data = {"protocol": self.protocol, "datasets": {}, "gate_matrix": self.gate_matrix["matrix"],
                "nsrr": {"error": self.nsrr_error}}
        for repository_id, stages in self.dataset_evidence.items():
            data["datasets"][repository_id] = {}
            for stage_name, entry in stages.items():
                manifest = entry["manifest"]
                directories = {key: inventory(source_path(self.root, manifest[key])) for key in
                    ("code_dir", "private_tests_dir", "regression_tests_dir", "reference_dir")}
                task_file = source_path(self.root, manifest["task_file"])
                data["datasets"][repository_id][stage_name] = {"manifest": manifest,
                    "directories": directories, "task_sha256": hashlib.sha256(task_file.read_bytes()).hexdigest(),
                    "stage_package": entry["stage_package"]}
        if self.nsrr_rows is not None:
            data["nsrr"]["rows"] = self.nsrr_rows
            input_manifest = getattr(self.nsrr_gate_evaluator, "input_manifest", None)
            if input_manifest is not None:
                data["nsrr"]["gate_input_manifest"] = input_manifest(self.batch_id, self.nsrr_rows)
        return {"sha256": digest(data), "inputs": data}

    def _ensure_live_ready(self):
        if not self.require_ready:
            return
        from scripts.validate_protocol import validate
        errors = validate(self.protocol, "formal_benchmark")
        if errors:
            raise ValueError("formal profile is not ready: " + "; ".join(errors))
        if self.nsrr_rows is None:
            raise ValueError("NSRR frozen audit is not ready: " + str(self.nsrr_error))
        if self.nsrr_gate_evaluator is None:
            raise ValueError("formal-ready blocked: EvoCode exposes no direct evaluator for frozen NSRR candidates; NSRR Gate decision is unverified")
        max_tokens = getattr(self.backend, "max_total_tokens", None)
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError("live formal execution requires explicit positive ModelClient token budget")

    def collection(self, owner: str):
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", owner)
        return "bench_" + self.batch_id + "_" + safe

    async def _bootstrap(self, owner: str):
        state = self.work / "states" / owner
        collection = self.collection(owner + "_initial")
        initial = await self.action(owner + ":bootstrap", {"state": str(state), "collection": collection},
            lambda: self.backend.initialize(state, collection))
        async def fingerprint_initial_state():
            fingerprint = getattr(self.backend, "state_fingerprint", None)
            if fingerprint:
                return {"state_sha256": fingerprint(state)}
            return {"state_sha256": digest(read_json(state / "state.json"))}
        fingerprint = await self.action(owner + ":initial_state_fingerprint",
            {"state": str(state), "initialize": initial}, fingerprint_initial_state)
        initial = {**initial, **fingerprint}
        async def audit():
            return await self.backend.audit(state, {"collection": initial["collection"], "history": initial["history"]})
        descriptor = await self.action(owner + ":initial_audit", initial, audit)
        return state, initial, descriptor

    def _attempt_id(self, condition: str, repository_id: str, stage_name: str) -> str:
        return f"{condition}_{repository_id}_{stage_name}"

    def _wave_entry(self, condition, repository_id, stage_name, state, history, descriptor,
                    *, scored=True, attempt_id=None, schedule_row=None, instance_history=None):
        attempt_id = attempt_id or self._attempt_id(condition, repository_id, stage_name)
        return {"condition": condition, "repository_id": repository_id, "stage_name": stage_name,
            "state": Path(state), "history": copy.deepcopy(history), "descriptor": copy.deepcopy(descriptor),
            "scored": scored, "attempt_id": attempt_id,
            "schedule_row": schedule_row or self.schedule_by_attempt.get(attempt_id),
            "instance_history": copy.deepcopy(instance_history) if instance_history is not None else copy.deepcopy(history)}

    async def _task(self, condition, repository_id, stage_name, state, history, descriptor,
                    *, scored=True, attempt_id=None, journal_metadata=None):
        stage = self.stage_directories[stage_name]
        attempt_id = attempt_id or self._attempt_id(condition, repository_id, stage_name)
        await super().start_task(condition, stage, state, history, descriptor, scored=scored,
            repository_id=repository_id, attempt_id=attempt_id, journal_metadata=journal_metadata)
        return await super().finalize_task(condition, stage, state, history, descriptor, scored=scored,
            repository_id=repository_id, attempt_id=attempt_id, journal_metadata=journal_metadata)

    async def _drain(self, awaitables):
        """Wait for every dispatched operation before propagating any failure."""
        values = await asyncio.gather(*awaitables, return_exceptions=True)
        failures = [value for value in values if isinstance(value, BaseException)]
        if failures:
            raise failures[0]
        return values

    async def _task_wave(self, entries: list[dict], wave_id: str) -> list[dict]:
        """Run an Agent wave behind start and terminal barriers, then grade in order."""
        if not entries:
            return []
        self.check_budget()
        attempts = [entry["attempt_id"] for entry in entries]
        if len(attempts) != len(set(attempts)):
            raise ValueError("task wave contains duplicate attempts")
        wave_order = list(attempts)
        stable_metadata = {}
        for entry in entries:
            schedule_row = entry.get("schedule_row") or {}
            stable_metadata[entry["attempt_id"]] = {
                "wave_id": wave_id,
                "wave_dispatch_order": wave_order,
                "dispatch_policy": "phase_barrier",
                "stage_start_history_sha256": digest(entry["history"]),
                "frozen_index_sha256": entry["descriptor"].get("content_sha256"),
                "pair_id": schedule_row.get("pair_id"),
                "pair_order": schedule_row.get("pair_order"),
                "pair_arm_order": schedule_row.get("pair_arm_order"),
            }
        for entry in entries:
            await super().prepare_task(entry["condition"], self.stage_directories[entry["stage_name"]],
                entry["state"], entry["history"], entry["descriptor"], scored=entry.get("scored", True),
                repository_id=entry["repository_id"], attempt_id=entry["attempt_id"],
                journal_metadata=stable_metadata[entry["attempt_id"]])

        # Fully journaled waves are replayed from their durable results only.
        # Their mutable source state may already contain later stages on resume,
        # so auditing it against an earlier stage-start index would be invalid.
        if all(self.journal.data["tasks"].get(entry["attempt_id"], {}).get("phase") == "recorded"
               for entry in entries):
            return [self.journal.data["tasks"][entry["attempt_id"]]["result"] for entry in entries]

        # Audit each unique state/index once before dispatch, then again after all
        # Agent tasks are terminal. No member can trigger incremental indexing.
        audited = set()
        for entry in entries:
            key = (str(Path(entry["state"]).resolve()), entry["descriptor"]["collection"])
            if key not in audited:
                await self.backend.audit(entry["state"], entry["descriptor"])
                audited.add(key)

        existing_wave_times = [self.journal.data["tasks"].get(attempt, {}).get("wave_dispatched_at")
                               for attempt in attempts]
        wave_dispatched_at = next((value for value in existing_wave_times if value is not None), time.time())
        pair_times = {}
        for entry in entries:
            schedule_row = entry.get("schedule_row") or {}
            pair_id = schedule_row.get("pair_id")
            if not pair_id:
                continue
            previous = self.journal.data["tasks"].get(entry["attempt_id"], {}).get("pair_dispatched_at")
            if previous is not None:
                pair_times.setdefault(pair_id, previous)
            else:
                pair_times.setdefault(pair_id, wave_dispatched_at)
        for entry in entries:
            attempt = entry["attempt_id"]
            row = self.journal.data["tasks"].get(attempt, {})
            values = {**stable_metadata[attempt], "wave_dispatched_at": row.get("wave_dispatched_at", wave_dispatched_at)}
            pair_id = values.get("pair_id")
            if pair_id:
                values["pair_dispatched_at"] = row.get("pair_dispatched_at", pair_times[pair_id])
            self.journal.update("tasks", attempt, **values)

        batch_entries = [{"state": entry["state"], "condition": entry["condition"],
            "history": entry["history"], "instance_history": entry.get("instance_history", entry["history"]),
            "collection": entry["descriptor"]["collection"], "repository_id": entry["repository_id"]}
            for entry in entries]

        async def dispatch():
            return await self._drain([self._start_wave_task(entry, stable_metadata[entry["attempt_id"]])
                                      for entry in entries])

        task_batch = getattr(self.backend, "task_batch", None)
        if task_batch is None:
            await dispatch()
        else:
            async with task_batch(batch_entries):
                # The LiveBackend context starts all isolated Instances and
                # waits for /api/health before this simultaneous dispatch.
                await dispatch()

        audited.clear()
        for entry in entries:
            key = (str(Path(entry["state"]).resolve()), entry["descriptor"]["collection"])
            if key not in audited:
                await self.backend.audit(entry["state"], entry["descriptor"])
                audited.add(key)

        # The entire Agent wave has drained. Grading runs in deterministic order
        # and cannot affect the runtime of another Agent in the same wave.
        results = []
        for entry in entries:
            result = await self._finalize_wave_task(entry, stable_metadata[entry["attempt_id"]])
            results.append(result)
        truncated = [row for row in results if row.get("termination") == "token_budget_exhausted"]
        budget = self.backend.budget()
        if truncated or budget.get("stop_reason"):
            raise BudgetStopped(budget)
        return results

    async def _start_wave_task(self, entry: dict, metadata: dict):
        stage = self.stage_directories[entry["stage_name"]]
        return await super().start_task(entry["condition"], stage, entry["state"], entry["history"],
            entry["descriptor"], scored=entry.get("scored", True), repository_id=entry["repository_id"],
            attempt_id=entry["attempt_id"], journal_metadata=metadata, audit_before=False, audit_after=False)

    async def _finalize_wave_task(self, entry: dict, metadata: dict):
        stage = self.stage_directories[entry["stage_name"]]
        return await super().finalize_task(entry["condition"], stage, entry["state"], entry["history"],
            entry["descriptor"], scored=entry.get("scored", True), repository_id=entry["repository_id"],
            attempt_id=entry["attempt_id"], journal_metadata=metadata)

    def _learning_plan(self, state, condition, stage_name, history, descriptor, repository_id, kind="task"):
        self.check_budget()
        if self.gate_provider is None:
            from runner.gate_tasks import build_gate_specs
            provider = build_gate_specs
        else:
            provider = self.gate_provider
        runtime = self.work / "gates" / condition / stage_name / (kind + ("_" + repository_id if repository_id else "_all"))
        stage = stage_name
        if kind == "meta":
            gate_specs = provider(self.root, stage, "meta", runtime / "meta", repository_id=None)
            regression_specs = []
            for repo in self.repositories:
                regression_specs += provider(self.root, stage, "regression", runtime / "regression" / repo,
                                             repository_id=repo)
            trace_ids = self._meta_trace_order(history["task_ids"])
            action_id = f"{self.batch_id}:{condition}:{stage_name}:meta:all_repositories"
            meta_run_ids = self._meta_history_run_order(condition)
            scope_repo = None
        else:
            gate_specs = provider(self.root, stage, "task", runtime / "task", repository_id=repository_id)
            regression_specs = provider(self.root, stage, "regression", runtime / "regression",
                                        repository_id=repository_id)
            trace_ids = list(history.get("task_ids_by_repository", {}).get(repository_id, []))
            action_id = f"{self.batch_id}:{condition}:{stage_name}:task:{repository_id}"
            meta_run_ids = None
            scope_repo = repository_id
        specs = [*gate_specs, *regression_specs]
        task_ids = [row.get("task_id") for row in specs]
        if not specs or None in task_ids or len(set(task_ids)) != len(task_ids):
            raise ValueError("formal Gate/regression matrix is empty or contains duplicate task IDs")
        if kind == "task" and any(row.get("repository_id") != repository_id for row in specs):
            raise ValueError("Task Gate/regression specs escaped their repository scope")
        if kind == "meta" and any(row.get("repository_id") not in self.repositories for row in specs):
            raise ValueError("Meta Gate/regression specs escaped the declared profile scope")
        journal_key = f"{condition}:{stage_name}:{kind}:{repository_id or 'all'}"
        return {"state": state, "condition": condition, "stage_name": stage_name,
            "history": history, "descriptor": descriptor, "trace_ids": trace_ids,
            "kind": kind, "action_id": action_id, "stage": stage, "specs": specs,
            "scope_repo": scope_repo, "journal_key": journal_key,
            "gate_task_ids": task_ids, "meta_history_run_ids": meta_run_ids}

    def _created_at(self, task_id):
        for row in self.journal.data["tasks"].values():
            result = row.get("result") or {}
            if result.get("agent_task_id") == task_id:
                value = result.get("agent_task_created_at") or row.get("agent_task_created_at")
                return value
            if row.get("agent_task_id") == task_id:
                return row.get("agent_task_created_at")
        return None

    def _meta_trace_order(self, task_ids):
        repository_order = {repository_id: index for index, repository_id in enumerate(self.repositories)}
        task_location = {}
        for attempt, row in self.journal.data["tasks"].items():
            result = row.get("result") or {}
            task_id = result.get("agent_task_id") or row.get("agent_task_id")
            if task_id:
                task_location[task_id] = row.get("schedule", {}).get("repository_id")
                if not task_location[task_id]:
                    task_location[task_id] = result.get("repository_id")
        def key(task_id):
            created = self._created_at(task_id)
            if not created:
                if not self.offline:
                    raise ValueError(f"Meta trace task lacks durable Agent created_at: {task_id}")
                created = datetime.min.replace(tzinfo=timezone.utc)
            try:
                timestamp = created if isinstance(created, datetime) else datetime.fromisoformat(
                    str(created).replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                if not self.offline:
                    raise ValueError(f"Meta trace task has invalid Agent created_at: {task_id}")
                timestamp = datetime.min.replace(tzinfo=timezone.utc)
            repository_id = task_location.get(task_id)
            return (timestamp, repository_order.get(repository_id, len(repository_order)), task_id)
        return sorted(task_ids, key=key)

    def _meta_history_run_order(self, condition):
        """Task learning runs ordered by stage, then frozen repository order."""
        result = []
        for stage_name in self.stage_names[:self.stage_names.index("transfer") + 1]:
            for repository_id in self.repositories:
                action_key = f"{condition}:{stage_name}:task:{repository_id}:prepare"
                action = self.journal.data["actions"].get(action_key, {})
                output = action.get("output") or {}
                run_id = output.get("run_id") or (output.get("run") or {}).get("run_id")
                if not run_id:
                    raise ValueError(f"B4 Meta history is missing Task learning run: {stage_name}/{repository_id}")
                result.append(run_id)
        return result

    def _learning_batch_context(self, plans, instance_history=None):
        entries = [{"state": plan["state"], "condition": plan["condition"],
            "history": plan["history"], "instance_history": instance_history or plan["history"],
            "collection": plan["descriptor"]["collection"],
            "repository_id": plan["scope_repo"] or self.repositories[0]} for plan in plans]
        return entries

    async def _prepare_learning_plan(self, plan):
        method = getattr(self.backend, "prepare_learning", None)
        if method is None:
            raise TypeError("formal backend must implement phased prepare_learning")
        key = plan["journal_key"] + ":prepare"
        inputs = self._learning_inputs(plan)
        async def prepare():
            return await method(plan["state"], plan["condition"], plan["history"],
                plan["descriptor"], plan["trace_ids"], plan["kind"], plan["action_id"],
                plan["stage"], plan["specs"], repository_id=plan["scope_repo"],
                meta_history_run_ids=plan["meta_history_run_ids"])
        return await self.action(key, inputs, prepare)

    async def _evaluate_learning_plan(self, plan, prepared):
        method = getattr(self.backend, "evaluate_learning", None)
        if method is None:
            raise TypeError("formal backend must implement phased evaluate_learning")
        key = plan["journal_key"] + ":evaluate"
        inputs = {**self._learning_inputs(plan), "run_id": prepared.get("run_id"),
            "candidate_version_id": prepared.get("candidate_version_id")}
        async def evaluate():
            return await method(plan["state"], plan["condition"], plan["history"],
                plan["descriptor"], prepared, plan["specs"], plan["kind"],
                repository_id=plan["scope_repo"])
        return await self.action(key, inputs, evaluate)

    async def _publish_learning_plan(self, plan, evaluation):
        method = getattr(self.backend, "publish_learning", None)
        if method is None:
            raise TypeError("formal backend must implement phased publish_learning")
        key = plan["journal_key"] + ":publish"
        inputs = {**self._learning_inputs(plan), "run_id": evaluation.get("run_id"),
            "status": evaluation.get("status"), "evaluation_passed": evaluation.get("evaluation_passed")}
        async def publish():
            return await method(plan["state"], plan["condition"], plan["history"],
                plan["descriptor"], evaluation, plan["kind"], repository_id=plan["scope_repo"])
        return await self.action(key, inputs, publish)

    def _learning_inputs(self, plan):
        return {"state": str(plan["state"]), "condition": plan["condition"],
            "stage": plan["stage"], "repository_id": plan["scope_repo"],
            "trace_task_ids": plan["trace_ids"], "history": plan["history"],
            "index": plan["descriptor"], "gate_task_ids": plan["gate_task_ids"],
            "kind": plan["kind"], "meta_history_run_ids": plan["meta_history_run_ids"]}

    @staticmethod
    def _assert_same_proposal_snapshot(prepared):
        snapshots = [row.get("proposal_snapshot") for row in prepared if row.get("proposal_snapshot") is not None]
        if snapshots and any(snapshot != snapshots[0] for snapshot in snapshots[1:]):
            raise ValueError("learning proposals did not use the same frozen active Skill/Meta snapshot")

    async def _run_learning_batch(self, plans, *, instance_history=None):
        if not plans:
            return []
        self.check_budget()
        replayed = self._replay_learning_batch(plans)
        if replayed is not None:
            return replayed
        context_entries = self._learning_batch_context(plans, instance_history)
        task_batch = getattr(self.backend, "task_batch", None)
        if task_batch is None:
            prepared = await self._drain([self._prepare_learning_plan(plan) for plan in plans])
            self._assert_same_proposal_snapshot(prepared)
            self.check_budget()
            candidates = [(plan, row) for plan, row in zip(plans, prepared, strict=True)
                          if row.get("status") == "candidate" and row.get("candidate_version_id")]
            evaluated_by_plan = {}
            if candidates:
                evaluations = await self._drain([self._evaluate_learning_plan(plan, row)
                    for plan, row in candidates])
                evaluated_by_plan.update({plan["journal_key"]: value
                    for (plan, _), value in zip(candidates, evaluations, strict=True)})
            self.check_budget()
            prepared_by_plan = {plan["journal_key"]: row
                                for plan, row in zip(plans, prepared, strict=True)}
            evaluation_by_plan = {plan["journal_key"]: evaluated_by_plan.get(plan["journal_key"], row)
                for plan, row in zip(plans, prepared, strict=True)}
            return await self._publish_learning_in_order(plans, prepared_by_plan, evaluation_by_plan)

        async with task_batch(context_entries):
            prepared = await self._drain([self._prepare_learning_plan(plan) for plan in plans])
            self._assert_same_proposal_snapshot(prepared)
            self.check_budget()
            candidates = [(plan, row) for plan, row in zip(plans, prepared, strict=True)
                          if row.get("status") == "candidate" and row.get("candidate_version_id")]
            evaluations = await self._drain([self._evaluate_learning_plan(plan, row)
                for plan, row in candidates]) if candidates else []
            self.check_budget()
            evaluated_by_plan = {plan["journal_key"]: value
                for (plan, _), value in zip(candidates, evaluations, strict=True)}
            prepared_by_plan = {plan["journal_key"]: row
                                for plan, row in zip(plans, prepared, strict=True)}
            evaluation_by_plan = {plan["journal_key"]: evaluated_by_plan.get(plan["journal_key"], row)
                for plan, row in zip(plans, prepared, strict=True)}
            # The publish barrier is after every evaluation returned terminal.
            return await self._publish_learning_in_order(plans, prepared_by_plan, evaluation_by_plan)

    def _replay_learning_batch(self, plans):
        """Reuse fully journaled learning phases without opening an Agent app."""
        outputs = []
        for plan in plans:
            prepare = self.journal.data["actions"].get(plan["journal_key"] + ":prepare", {})
            if prepare.get("status") != "completed":
                return None
            prepared = prepare.get("output") or {}
            if prepared.get("status") == "candidate" and prepared.get("candidate_version_id"):
                publish = self.journal.data["actions"].get(plan["journal_key"] + ":publish", {})
                if publish.get("status") != "completed":
                    return None
                outputs.append(publish.get("output") or {})
            else:
                outputs.append(prepared)
        return outputs

    async def _publish_learning_in_order(self, plans, prepared_by_plan, evaluation_by_plan):
        published = []
        for plan in plans:
            prepared = prepared_by_plan[plan["journal_key"]]
            evaluation = evaluation_by_plan[plan["journal_key"]]
            if prepared.get("status") != "candidate" or not prepared.get("candidate_version_id"):
                published.append(prepared)
                continue
            published.append(await self._publish_learning_plan(plan, evaluation))
        return published

    async def _learn_task_batch(self, state, condition, stage_name, histories, descriptor, stage_history):
        plans = [self._learning_plan(state, condition, stage_name, histories[repository_id],
            descriptor, repository_id, "task") for repository_id in self.repositories]
        return await self._run_learning_batch(plans, instance_history=stage_history)

    async def _learn(self, state, condition, stage_name, history, descriptor, repository_id, kind="task"):
        plan = self._learning_plan(state, condition, stage_name, history, descriptor, repository_id, kind)
        outcomes = await self._run_learning_batch([plan], instance_history=history)
        return outcomes[0]

    def _batch_manifest(self):
        inputs = self._input_fingerprint()
        nsrr_gate_inputs = None
        if self.nsrr_rows is not None and self.nsrr_gate_evaluator is not None:
            nsrr_gate_inputs = self.nsrr_gate_evaluator.input_manifest(self.batch_id, self.nsrr_rows)
        return {"protocol_id": self.protocol["protocol_id"], "profile": "formal_benchmark",
            "formal_results": not self.offline, "offline_contract_test": self.offline,
            "batch_id": self.batch_id, "repeat_id": 1, "seed": 42, "seed_supported": self._seed_supported,
            "seed_status": "unsupported_by_current_agent_task_api",
            "planned_main_positions": 54, "execution_schedule": self.schedule,
            "repository_order": self.repositories, "dataset_fingerprint": inputs["sha256"],
            "gate_matrix": self.gate_matrix["matrix"], "nsrr_audit_task_ids": self.profile.get("nsrr", {}).get("audit_task_ids"),
            "nsrr_gate_inputs": nsrr_gate_inputs,
            "nsrr_policy_files_sent_to_gate": False,
            "nsrr_frozen_input_status": "verified" if self.nsrr_rows is not None else "unverified",
            "nsrr_input_error": self.nsrr_error, "metadata": self.metadata,
            "dispatch_policy": self.protocol["execution"]["dispatch_policy"],
            "sqlite_concurrency": self.protocol["execution"]["sqlite_concurrency"],
            "parallelism": self.profile["parallelism"],
            "wave_order": "repository_order_then_recorded_A_arm_order",
            "agent_grading_policy": "after_all_wave_tasks_terminal_in_repository_order",
            "token_budget_policy": "one_shared_cross_process_ledger; admission_is_first_reservation_wins; budget_truncation_invalidates_complete",
            "initial_state_origin": "fresh identical bootstrap per B condition; Task Skills empty; built-in Meta V1"}

    async def run(self):
        self._ensure_live_ready()
        with (self.results / ".runner.lock").open("a") as lock:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("another runner owns this batch") from None
            return await self._run_locked()

    async def _run_locked(self):
        manifest = self._batch_manifest()
        manifest_file = self.results / "batch_manifest.json"
        if manifest_file.exists() and read_json(manifest_file) != manifest:
            raise ValueError("batch resume fingerprint changed; use a new batch_id")
        atomic_json(manifest_file, manifest)
        for row in self.schedule:
            if row["attempt_id"] not in self.journal.data["tasks"]:
                self.journal.update("tasks", row["attempt_id"], phase="planned", schedule=row,
                    inputs_fingerprint=manifest["dataset_fingerprint"])
        stop = None
        try:
            stop = await self._execute_formal_matrix()
        except BudgetStopped as exc:
            stop = {"reason": "token_budget_limit", "budget": exc.state}
        except BaseException as exc:
            stop = {"reason": "runner_interrupted", "error_type": type(exc).__name__}
            self.write_summary(stop)
            raise
        return self.write_summary(stop)

    async def _execute_formal_matrix(self):
        empty_history = {"task_ids": [], "event_ids": [],
            "task_ids_by_repository": {repo: [] for repo in self.repositories},
            "event_ids_by_repository": {repo: [] for repo in self.repositories}}

        # One actual, ungraded public history is collected and frozen for A1/A2.
        collector, initial, descriptor = await self._bootstrap("public_history")
        public_history = copy.deepcopy(initial["history"])
        public_history.update(copy.deepcopy(empty_history))
        public = {}
        for stage_name in self.stage_names[:3]:
            stage_start_history = copy.deepcopy(public_history)
            next_history = copy.deepcopy(public_history)
            entries = [self._wave_entry("collector", repository_id, stage_name, collector,
                stage_start_history, descriptor, scored=False,
                attempt_id=f"collector_{repository_id}_{stage_name}") for repository_id in self.repositories]
            results = await self._task_wave(entries, f"collector_{stage_name}")
            for repository_id, result in zip(self.repositories, results, strict=True):
                next_history = self.history_after(next_history, result, repository_id)
            public_history = next_history
            descriptor = await self.index_stage(collector, "collector", self.stage_directories[stage_name],
                public_history, descriptor)
            public[stage_name] = await self.freeze(collector, "public_history_" + stage_name,
                public_history, descriptor)

        # Revalidate only actual public-history evidence, scoped per repository.
        for repository_id in self.repositories:
            target = self.work / "history_revalidation" / repository_id
            if not target.exists():
                manifest = self.manifests[repository_id]["t3_stale"]
                from runner.stage_packages import materialize_stage_package
                materialize_stage_package(self.root, manifest, target)
            transfer = public["transfer"]
            ids = transfer["history"].get("task_ids_by_repository", {}).get(repository_id, [])
            await self.action("public_history:" + repository_id + ":t3_applicability", {
                "snapshot": transfer["state"], "repository_id": repository_id, "trace_task_ids": ids},
                lambda repo=repository_id, state=transfer["state"], trace=ids, workspace=target:
                    self.backend.history_evidence(Path(state), trace, workspace, repository_id=repo))

        # A1/A2 clone the exact same per-stage public state for each task.
        for stage_name in self.protocol["experiment_a"]["scored_stages"]:
            snapshot_name = self.stage_names[self.stage_names.index(stage_name) - 1]
            frozen = public[snapshot_name]
            entries = []
            for repository_id in self.repositories:
                pair_rows = sorted((row for row in self.schedule if row["experiment"] == "A"
                    and row["stage_name"] == stage_name and row["repository_id"] == repository_id),
                    key=lambda row: row["pair_order"])
                if [row["condition"] for row in pair_rows] != pair_rows[0]["pair_arm_order"]:
                    raise ValueError("A pair schedule order disagrees with the recorded arm order")
                for row in pair_rows:
                    condition = row["condition"]
                    attempt = self._attempt_id(condition, repository_id, stage_name)
                    state = self.work / "states" / "A" / attempt
                    await self.clone(Path(frozen["state"]), state, attempt)
                    entries.append(self._wave_entry(condition, repository_id, stage_name, state,
                        frozen["history"], frozen["index"], schedule_row=row))
            await self._task_wave(entries, f"A_{stage_name}")

        # B groups each start from independent equivalent initialized states.
        b_order = []
        for row in self.schedule:
            if row["experiment"] == "B" and row["condition"] not in b_order:
                b_order.append(row["condition"])
        initial_fingerprints = {}
        for condition in b_order:
            state, initial, descriptor = await self._bootstrap("initial_" + condition)
            initial_fingerprints[condition] = initial["state_sha256"]
            skills = initial.get("task_skills", [])
            if skills:
                raise ValueError("B initial Task Skill library must be empty")
            if condition == b_order[0]:
                shared_initial = (initial_fingerprints[condition], initial.get("meta"))
            elif initial_fingerprints[condition] != shared_initial[0] or initial.get("meta") != shared_initial[1]:
                raise ValueError("B groups did not receive equivalent independent initial states")

            history = copy.deepcopy(initial["history"])
            history.update(copy.deepcopy(empty_history))
            repo_task_ids = {repo: [] for repo in self.repositories}
            previous_freeze = None
            live_state = state
            for stage_name in self.stage_names:
                if stage_name == "heldout":
                    if previous_freeze is None:
                        raise ValueError("T4 has no frozen group state")
                    entries = []
                    for repository_id in self.repositories:
                        attempt = self._attempt_id(condition, repository_id, stage_name)
                        heldout = self.work / "states" / "heldout" / attempt
                        await self.clone(Path(previous_freeze["state"]), heldout, attempt)
                        entries.append(self._wave_entry(condition, repository_id, stage_name, heldout,
                            previous_freeze["history"], previous_freeze["index"]))
                    await self._task_wave(entries, f"B_{condition}_heldout")
                    continue

                stage_start = copy.deepcopy(history)
                next_history = copy.deepcopy(history)
                entries = [self._wave_entry(condition, repository_id, stage_name, live_state,
                    stage_start, descriptor) for repository_id in self.repositories]
                results = await self._task_wave(entries, f"B_{condition}_{stage_name}")
                for repository_id, result in zip(self.repositories, results, strict=True):
                    next_history = self.history_after(next_history, result, repository_id)
                    repo_task_ids[repository_id].append(result["agent_task_id"])
                history = next_history
                descriptor = await self.index_stage(live_state, condition, self.stage_directories[stage_name],
                    history, descriptor)
                if condition in ("B3", "B4"):
                    task_histories = {}
                    for repository_id in self.repositories:
                        repo_history = copy.deepcopy(history)
                        repo_history["task_ids"] = list(repo_task_ids[repository_id])
                        repo_history["task_ids_by_repository"] = {repository_id: list(repo_task_ids[repository_id])}
                        task_histories[repository_id] = repo_history
                    await self._learn_task_batch(live_state, condition, stage_name,
                        task_histories, descriptor, history)
                if stage_name == "transfer":
                    if condition == "B4":
                        if any(len(repo_task_ids[repo]) < 2 for repo in self.repositories):
                            raise ValueError("B4 Meta barrier requires all three repositories through T2")
                        await self._learn(live_state, condition, stage_name, history, descriptor, None, kind="meta")
                    else:
                        await self.action(condition + ":transfer:meta_barrier", {
                            "repositories": self.repositories, "history": history},
                            lambda: _return_fixed_meta_status())
                previous_freeze = await self.freeze(live_state, condition + "_" + stage_name, history, descriptor)
                if stage_name != "heldout":
                    # Further in-sample tasks continue from the mutable group state;
                    # the freeze is a separately owned immutable clone.
                    pass

        self.journal.update("actions", "formal:initial_state_equivalence", status="completed",
            output={"fingerprints": initial_fingerprints, "independent_group_states": True})
        if self.nsrr_rows is not None and self.nsrr_gate_evaluator is not None:
            try:
                input_manifest = getattr(self.nsrr_gate_evaluator, "input_manifest", None)
                if input_manifest is None:
                    raise ValueError("NSRR evaluator must provide stable journal inputs")
                audit_inputs = input_manifest(self.batch_id, self.nsrr_rows)
                response = await self.action("formal:nsrr_gate_batch", audit_inputs,
                    lambda: self.nsrr_gate_evaluator.evaluate(self.root, self.batch_id, self.nsrr_rows))
                if response.get("batch_id") != self.batch_id or response.get("status") not in {"completed", "failed"}:
                    raise ValueError("NSRR Gate API batch has not reached a terminal state")
                records = response.get("records")
                if not isinstance(records, list):
                    raise ValueError("NSRR Gate API completed response lacks records")
                self.nsrr_decisions = validate_nsrr_gate_decisions(records, self.batch_id)
                expected = {row["candidate_id"] for row in self.nsrr_rows}
                actual = {row["candidate_id"] for row in self.nsrr_decisions}
                if actual != expected:
                    self.nsrr_error = "NSRR Gate API record matrix differs from the frozen eight-candidate matrix"
                elif response.get("status") == "failed":
                    self.nsrr_error = "NSRR Gate API batch completed with failed candidate evaluations"
            except Exception as exc:
                self.nsrr_error = f"{type(exc).__name__}: {exc}"
        return None

    def write_summary(self, stop):
        all_rows = []
        for row in self.schedule:
            state = self.journal.data["tasks"].get(row["attempt_id"], {})
            result = state.get("result") if state.get("phase") == "recorded" else None
            all_rows.append({**row, "attempt_state": state.get("phase", "planned"),
                "agent_task_id": result.get("agent_task_id") if result else None,
                "agent_status": result.get("agent_status") if result else None,
                "termination": result.get("termination") if result else None,
                "grade_status": result.get("grade", {}).get("status") if result else None,
                "solved": result.get("solved") if result else None,
                "agent_dispatched_at": state.get("agent_dispatched_at"),
                "agent_task_created_at": (result or {}).get("agent_task_created_at") or state.get("agent_task_created_at"),
                "agent_terminal_at": state.get("agent_terminal_at"),
                "wave_dispatched_at": state.get("wave_dispatched_at"),
                "pair_dispatched_at": state.get("pair_dispatched_at"),
                "dispatch": (result or {}).get("dispatch") or {key: state.get(key) for key in (
                    "wave_id", "dispatch_policy", "wave_dispatch_order", "pair_id", "pair_order",
                    "pair_arm_order", "wave_dispatched_at", "pair_dispatched_at",
                    "agent_dispatched_at", "agent_terminal_at", "agent_task_created_at") if key in state},
                "artifact": result.get("artifact") if result else None,
                "result": result})
        recorded = [row["result"] for row in all_rows if row["result"] is not None]
        verified = [row for row in recorded if row.get("grade", {}).get("status") == "graded"]
        unverified_rows = [
            {key: row[key] for key in ("position", "attempt_id", "condition", "repository_id", "stage", "task_id")}
            | {
                "agent_status": row.get("agent_status"),
                "termination": row.get("termination"),
                "reason": ((row.get("result") or {}).get("grade") or {}).get("reason", "unverified"),
                "error": ((row.get("result") or {}).get("grade") or {}).get("error"),
            }
            for row in all_rows
            if row.get("grade_status") == "unverified"
        ]
        missing = [{key: row[key] for key in ("position", "attempt_id", "condition", "repository_id", "stage", "task_id")}
            | {"state": row["attempt_state"], "reason": stop.get("reason") if stop else "not_dispatched_or_interrupted"}
            for row in all_rows if row["result"] is None]
        decisions = self.nsrr_decisions
        nsrr = summarize_nsrr(self.nsrr_rows, decisions) if self.nsrr_rows is not None else {
            "status": "unverified", "value": None, "formula": "K/N", "reason": self.nsrr_error,
            "audit_task_ids": self.profile.get("nsrr", {}).get("audit_task_ids")}
        if self.nsrr_gate_evaluator is None and self.nsrr_rows is not None:
            nsrr = {**nsrr, "status": "unverified", "value": None,
                "reason": "EvoCode has no direct API for evaluating frozen NSRR candidates; main-learning records are not used as a substitute"}
        elif self.nsrr_error:
            nsrr = {**nsrr, "api_error": self.nsrr_error}
        budget = self.backend.budget()
        budget_exhausted_attempts = [row["attempt_id"] for row in recorded
            if row.get("termination") == "token_budget_exhausted"]
        budget_truncated = bool(budget.get("stop_reason") or budget_exhausted_attempts
            or (stop and stop.get("reason") == "token_budget_limit"))

        def timestamp(value):
            if not value:
                return None
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
            except ValueError:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None

        paired_dispatches = []
        for pair_id in sorted({row["pair_id"] for row in all_rows if row.get("pair_id")}):
            members = sorted((row for row in all_rows if row.get("pair_id") == pair_id),
                key=lambda row: row.get("pair_order") or 0)
            values = [row.get("agent_task_created_at") or row.get("agent_dispatched_at") for row in members]
            times = [timestamp(value) for value in values]
            observed = [value for value in times if value is not None]
            paired_dispatches.append({"pair_id": pair_id,
                "pair_arm_order": members[0].get("pair_arm_order") if members else [],
                "attempts": [{"attempt_id": row["attempt_id"], "condition": row["condition"],
                    "pair_order": row.get("pair_order"), "pair_dispatched_at": row.get("pair_dispatched_at"),
                    "agent_dispatched_at": row.get("agent_dispatched_at"),
                    "agent_task_created_at": row.get("agent_task_created_at"),
                    "attempt_state": row["attempt_state"], "termination": row.get("termination")}
                    for row in members],
                "start_skew_seconds": max(observed) - min(observed) if len(observed) == len(members) and observed else None,
                "complete_pair": len(members) == 2 and all(row.get("result") is not None
                    and row.get("termination") != "token_budget_exhausted" for row in members)})
        actual_waves = []
        for wave_id in sorted({row.get("wave_id") for row in all_rows if row.get("wave_id")}):
            members = [row for row in all_rows if row.get("wave_id") == wave_id]
            actual_waves.append({"wave_id": wave_id,
                "planned_attempts": [row["attempt_id"] for row in members],
                "dispatched_attempts": [row["attempt_id"] for row in members if row.get("agent_dispatched_at")],
                "terminal_attempts": [row["attempt_id"] for row in members if row.get("agent_terminal_at")],
                "wave_dispatched_at": next((row.get("wave_dispatched_at") for row in members
                    if row.get("wave_dispatched_at") is not None), None),
                "terminal_barrier_satisfied": bool(members) and all(row.get("agent_terminal_at") for row in members),
                "repository_order": [row["repository_id"] for row in members]})
        learning_actions = [{"key": key, "status": value.get("status"),
            "run_id": (value.get("output") or {}).get("run_id"),
            "candidate_version_id": (value.get("output") or {}).get("candidate_version_id")}
            for key, value in sorted(self.journal.data["actions"].items())
            if any(token in key for token in (":prepare", ":evaluate", ":publish"))]
        concurrency = {"dispatch_policy": self.protocol["execution"]["dispatch_policy"],
            "parallelism": self.profile["parallelism"], "actual_waves": actual_waves,
            "a_pairs": paired_dispatches, "learning_actions": learning_actions,
            "fixed_result_merge_order": self.repositories,
            "meta_trace_order": "task_created_at_then_repository_order_then_task_id",
            "agent_grading": "after_all_wave_tasks_terminal_in_repository_order",
            "shared_token_budget_ledger": True, "budget_truncated": budget_truncated,
            "budget_exhausted_attempts": budget_exhausted_attempts,
            "paired_complete_count": sum(pair["complete_pair"] for pair in paired_dispatches),
            "paired_planned_count": len(paired_dispatches)}
        metrics = compute_formal_metrics(all_rows, self.journal.data.get("actions", {}), self.repositories, nsrr)
        gate_eval_path = self.results / "gate_evaluations.json"
        atomic_json(gate_eval_path, decisions)
        atomic_json(self.results / "planned_matrix.json", self.schedule)
        atomic_json(self.results / "attempt_states.json", all_rows)
        atomic_json(self.results / "missing_ledger.json", missing)
        atomic_json(self.results / "unverified_ledger.json", unverified_rows)
        atomic_json(self.results / "metrics.json", metrics)
        atomic_json(self.results / "nsrr.json", {"audit_rows": self.nsrr_rows or [], "summary": nsrr,
            "gate_decisions": decisions,
            "source_boundary": "NSRR labels are read-only; direct Gate runs may enter the audit-only Evolution/evaluation ledger, but do not create main task events, write Raw-RAG, change the Skill Registry, or enter later learning history; frozen policy.json files inform offline labels only and are not sent to Gate"})
        summary = {"kind": "offline_formal_contract_test" if self.offline else "formal_benchmark",
            "formal_results": not self.offline, "offline_contract_test": self.offline,
            "protocol_id": self.protocol["protocol_id"], "profile": "formal_benchmark", "batch_id": self.batch_id,
            "repeat_id": 1, "seed": 42, "seed_supported": False,
            "planned": 54, "recorded": len(recorded), "verified": len(verified),
            "unverified": len(unverified_rows), "unverified_ledger": unverified_rows,
            "coverage": len(verified) / 54, "solved": sum(bool(row.get("solved")) for row in verified),
            "effective_denominator": len(verified),
            "solve_rate": sum(bool(row.get("solved")) for row in verified) / len(verified) if verified else None,
            "successes_over_planned": sum(bool(row.get("solved")) for row in verified) / 54,
            "execution_complete": len(recorded) == 54 and not stop and not budget_truncated,
            "complete": (not self.offline and len(recorded) == 54 and len(verified) == 54 and not stop
                         and not budget_truncated
                         and nsrr.get("status") in {"calculated", "not_applicable"}
                         and nsrr.get("matrix_status") == "complete"),
            "missing_count": len(missing), "missing_ledger": missing,
            "attempt_states": [{key: row[key] for key in ("position", "attempt_id", "condition", "repository_id", "stage", "task_id", "attempt_state")} for row in all_rows],
            "planned_matrix": self.schedule, "results": recorded, "nsrr": nsrr,
            "metrics": metrics,
            "gate_evaluations": str(gate_eval_path), "stop": stop,
            "budget": budget, "concurrency": concurrency, "metadata": self.metadata,
            "boundary": "Offline fake backend only; no model calls, embeddings, rerank, external inference, or ability scores." if self.offline
                else "Single formal execution; report results only from recorded Agent and independent Grader outputs."}
        summary_path = self.results / "summary.json"
        atomic_json(summary_path, relative_paths(summary, self.root))
        if not self.offline:
            report_dir = self.root / "reports" / self.batch_id
            report_dir.mkdir(parents=True, exist_ok=True)
            fields = ["position", "attempt_id", "condition", "repository_id", "stage", "task_id", "attempt_state",
                      "agent_task_id", "agent_status", "termination", "grade_status", "solved"]
            with (report_dir / "main_tasks.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for row in all_rows:
                    writer.writerow({key: row.get(key) for key in fields})
            cond_fields = [
                "condition", "planned", "recorded", "verified", "unverified", "coverage",
                "solved", "solve_rate", "tool_calls_per_solved", "tokens_per_solved",
                "total_online_tokens", "learning_tokens", "total_end_to_end_tokens",
                "regression_rate", "candidates_proposed", "candidates_published", "candidates_blocked",
            ]
            with (report_dir / "condition_metrics.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=cond_fields)
                writer.writeheader()
                for cond_name, cond_row in metrics["by_condition"].items():
                    writer.writerow({
                        "condition": cond_name,
                        "planned": cond_row["planned"],
                        "recorded": cond_row["recorded"],
                        "verified": cond_row["verified"],
                        "unverified": cond_row["unverified"],
                        "coverage": cond_row["coverage"],
                        "solved": cond_row["solved"],
                        "solve_rate": cond_row["solve_rate"],
                        "tool_calls_per_solved": cond_row["tool_calls_per_solved"],
                        "tokens_per_solved": cond_row["tokens_per_solved"],
                        "total_online_tokens": cond_row["total_online_tokens"],
                        "learning_tokens": cond_row["learning"]["total_learning_tokens"],
                        "total_end_to_end_tokens": cond_row["total_cost"]["total_tokens"],
                        "regression_rate": cond_row["regression_rate"],
                        "candidates_proposed": cond_row["learning"]["candidates_proposed"],
                        "candidates_published": cond_row["learning"]["candidates_published"],
                        "candidates_blocked": cond_row["learning"]["candidates_blocked"],
                    })
            lines = render_formal_markdown_report(summary, all_rows, metrics, nsrr, stop)
            (report_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
        return summary


def _online_tokens(result: dict | None) -> int | None:
    if not isinstance(result, dict):
        return None
    telemetry = result.get("telemetry") or {}
    tokens = telemetry.get("reported_tokens")
    if type(tokens) is int:
        return tokens
    known = telemetry.get("known_reported_tokens")
    if type(known) is int:
        return known
    return None


def _online_tool_calls(result: dict | None) -> int | None:
    if not isinstance(result, dict):
        return None
    telemetry = result.get("telemetry") or {}
    calls = telemetry.get("tool_calls")
    return calls if type(calls) is int else None


def _summarize_subset(rows_subset: list[dict]) -> dict:
    """计算一组位置的成功率、有效分母、平均成本与回归率。"""
    planned = len(rows_subset)
    recorded_rows = [r for r in rows_subset if r.get("result") is not None]
    verified_rows = [r for r in recorded_rows if r.get("grade_status") == "graded"]
    unverified_rows = [r for r in recorded_rows if r.get("grade_status") == "unverified"]
    solved_rows = [r for r in verified_rows if r.get("solved") is True]
    valid_solved_tools = [v for v in (_online_tool_calls(r.get("result")) for r in solved_rows) if v is not None]
    valid_solved_tokens = [v for v in (_online_tokens(r.get("result")) for r in solved_rows) if v is not None]
    all_verified_tools = [v for v in (_online_tool_calls(r.get("result")) for r in verified_rows) if v is not None]
    all_verified_tokens = [v for v in (_online_tokens(r.get("result")) for r in verified_rows) if v is not None]
    reg_passes = []
    for r in verified_rows:
        reg = (((r.get("result") or {}).get("grade") or {}).get("suites") or {}).get("regression")
        if isinstance(reg, dict) and isinstance(reg.get("passed"), bool):
            reg_passes.append(reg["passed"])
    return {
        "planned": planned,
        "recorded": len(recorded_rows),
        "verified": len(verified_rows),
        "unverified": len(unverified_rows),
        "coverage": len(verified_rows) / planned if planned else None,
        "solved": len(solved_rows),
        "solve_rate": len(solved_rows) / len(verified_rows) if verified_rows else None,
        "successes_over_planned": len(solved_rows) / planned if planned else None,
        "tool_calls_per_solved": sum(valid_solved_tools) / len(valid_solved_tools) if valid_solved_tools else None,
        "tokens_per_solved": sum(valid_solved_tokens) / len(valid_solved_tokens) if valid_solved_tokens else None,
        "total_online_tool_calls": sum(all_verified_tools) if all_verified_tools else 0,
        "total_online_tokens": sum(all_verified_tokens) if all_verified_tokens else None,
        "regression_evaluated": len(reg_passes),
        "regression_passed": sum(1 for p in reg_passes if p),
        "regression_failed": sum(1 for p in reg_passes if not p),
        "regression_rate": (sum(1 for p in reg_passes if not p) / len(reg_passes)) if reg_passes else None,
    }


def _summarize_learning_for_condition(condition: str, actions: dict) -> dict:
    """汇总实验组的后台学习、门禁评估与回归拦截统计。"""
    base_keys = sorted({
        key.rsplit(":", 1)[0]
        for key in actions
        if key.startswith(f"{condition}:") and key.endswith((":prepare", ":evaluate", ":publish"))
    })
    proposal_tokens = 0
    gate_tokens = 0
    gate_tool_calls = 0
    candidates_proposed = 0
    candidates_published = 0
    candidates_blocked = 0
    gate_reg_checks = 0
    gate_reg_failures = 0
    for base in base_keys:
        prep = (actions.get(base + ":prepare") or {}).get("output") or {}
        eval_out = (actions.get(base + ":evaluate") or {}).get("output") or {}
        pub = (actions.get(base + ":publish") or {}).get("output") or {}
        final = pub or eval_out or prep
        run = final.get("run") or eval_out.get("run") or prep.get("run") or {}
        payload = (run.get("payload") if isinstance(run, dict) else None) or {}
        usage_tokens = (payload.get("usage") or {}).get("total_tokens")
        if type(usage_tokens) is int:
            proposal_tokens += usage_tokens
        meta_stage_tokens = (payload.get("meta_stage_usage") or {}).get("total_tokens")
        if type(meta_stage_tokens) is int:
            proposal_tokens += meta_stage_tokens
        evaluation = payload.get("evaluation") if isinstance(payload.get("evaluation"), dict) else {}
        for branch in ("baseline", "candidate"):
            b_metrics = evaluation.get(branch) if isinstance(evaluation.get(branch), dict) else {}
            if type(b_metrics.get("tokens")) is int:
                gate_tokens += b_metrics["tokens"]
            if type(b_metrics.get("tool_calls")) is int:
                gate_tool_calls += b_metrics["tool_calls"]
        for t_row in evaluation.get("tasks") or []:
            if not isinstance(t_row, dict):
                continue
            t_id = str(t_row.get("task_id") or "")
            if "_replay_" in t_id or t_row.get("gate_kind") == "regression":
                gate_reg_checks += 1
                c_pass = (t_row.get("candidate") or {}).get("passed")
                if c_pass is False:
                    gate_reg_failures += 1
        cand_id = final.get("candidate_version_id") or prep.get("candidate_version_id")
        status = final.get("status") or (run.get("status") if isinstance(run, dict) else None)
        if cand_id:
            candidates_proposed += 1
            if status == "published":
                candidates_published += 1
            elif status in {"evaluated", "needs_review", "failed", "not_evaluated"}:
                candidates_blocked += 1
    return {
        "learning_actions_count": len(base_keys),
        "proposal_tokens": proposal_tokens,
        "gate_tokens": gate_tokens,
        "gate_tool_calls": gate_tool_calls,
        "total_learning_tokens": proposal_tokens + gate_tokens,
        "candidates_proposed": candidates_proposed,
        "candidates_published": candidates_published,
        "candidates_blocked": candidates_blocked,
        "gate_regression_checks": gate_reg_checks,
        "gate_regression_failures": gate_reg_failures,
        "gate_regression_rate": (gate_reg_failures / gate_reg_checks) if gate_reg_checks else None,
    }


def compute_formal_metrics(all_rows: list[dict], actions: dict, repositories: list[str], nsrr: dict) -> dict:
    """按冻结协议计算分组、分阶段、迁移率、共同成功题配对成本与回归率指标。"""
    conditions = (*CONDITIONS_A, *CONDITIONS_B)
    by_condition: dict[str, dict] = {}
    for condition in conditions:
        cond_rows = [r for r in all_rows if r.get("condition") == condition]
        base_stats = _summarize_subset(cond_rows)
        learning = _summarize_learning_for_condition(condition, actions)
        stages = ("transfer", "stale", "heldout") if condition in CONDITIONS_A else ("seed", "transfer", "stale", "heldout")
        by_stage = {
            stage_name: _summarize_subset([r for r in cond_rows if r.get("stage_name") == stage_name])
            for stage_name in stages
        }
        online_tokens = base_stats["total_online_tokens"]
        total_tokens = (online_tokens or 0) + learning["total_learning_tokens"] if (online_tokens is not None or learning["total_learning_tokens"] > 0) else None
        total_tools = base_stats["total_online_tool_calls"] + learning["gate_tool_calls"]
        solved_count = base_stats["solved"]
        by_condition[condition] = {
            **base_stats,
            "learning": learning,
            "total_cost": {
                "total_tokens": total_tokens,
                "total_tokens_per_solved": (total_tokens / solved_count) if (solved_count > 0 and total_tokens is not None) else None,
                "total_tool_calls": total_tools,
                "total_tool_calls_per_solved": (total_tools / solved_count) if solved_count > 0 else None,
            },
            "by_stage": by_stage,
        }

    def _rate_delta(right_cond: str, left_cond: str, stage_name: str | None = None) -> float | None:
        if stage_name is None:
            r_rate = by_condition[right_cond]["solve_rate"]
            l_rate = by_condition[left_cond]["solve_rate"]
        else:
            r_rate = by_condition[right_cond]["by_stage"].get(stage_name, {}).get("solve_rate")
            l_rate = by_condition[left_cond]["by_stage"].get(stage_name, {}).get("solve_rate")
        if r_rate is None or l_rate is None:
            return None
        return r_rate - l_rate

    transfer_by_condition = {}
    for condition in CONDITIONS_B:
        cond_rows = [r for r in all_rows if r.get("condition") == condition]
        t2_stats = by_condition[condition]["by_stage"]["transfer"]
        t4_stats = by_condition[condition]["by_stage"]["heldout"]
        t2_t4_stats = _summarize_subset([r for r in cond_rows if r.get("stage_name") in {"transfer", "heldout"}])
        post_meta_stats = _summarize_subset([r for r in cond_rows if r.get("stage_name") in {"stale", "heldout"}])
        transfer_by_condition[condition] = {
            "t2_transfer": t2_stats,
            "t4_heldout": t4_stats,
            "combined_t2_t4": t2_t4_stats,
            "post_meta_t3_t4": post_meta_stats,
        }

    def _combined_delta(right_cond: str, left_cond: str, key: str) -> float | None:
        r_rate = transfer_by_condition[right_cond][key]["solve_rate"]
        l_rate = transfer_by_condition[left_cond][key]["solve_rate"]
        if r_rate is None or l_rate is None:
            return None
        return r_rate - l_rate

    skill_transfer = {
        "by_condition": transfer_by_condition,
        "deltas": {
            "b3_minus_b2_t2_solve_rate": _rate_delta("B3", "B2", "transfer"),
            "b4_minus_b3_t2_solve_rate": _rate_delta("B4", "B3", "transfer"),
            "b3_minus_b2_t4_solve_rate": _rate_delta("B3", "B2", "heldout"),
            "b4_minus_b3_t4_solve_rate": _rate_delta("B4", "B3", "heldout"),
            "b3_minus_b2_combined_t2_t4_solve_rate": _combined_delta("B3", "B2", "combined_t2_t4"),
            "b4_minus_b3_combined_t2_t4_solve_rate": _combined_delta("B4", "B3", "combined_t2_t4"),
            "b4_minus_b3_post_meta_t3_t4_solve_rate": _combined_delta("B4", "B3", "post_meta_t3_t4"),
        },
    }

    paired_solved_cost: dict[str, dict] = {}
    for left_cond, right_cond in (("A1", "A2"), ("B2", "B3"), ("B3", "B4"), ("B2", "B4")):
        left_map = {
            (r["repository_id"], r["stage_name"]): r
            for r in all_rows
            if r.get("condition") == left_cond and r.get("grade_status") == "graded" and r.get("solved") is True
        }
        right_map = {
            (r["repository_id"], r["stage_name"]): r
            for r in all_rows
            if r.get("condition") == right_cond and r.get("grade_status") == "graded" and r.get("solved") is True
        }
        common_keys = sorted(set(left_map) & set(right_map), key=lambda item: (repositories.index(item[0]) if item[0] in repositories else 99, item[1]))
        l_tools = [v for v in (_online_tool_calls(left_map[k].get("result")) for k in common_keys) if v is not None]
        r_tools = [v for v in (_online_tool_calls(right_map[k].get("result")) for k in common_keys) if v is not None]
        l_tokens = [v for v in (_online_tokens(left_map[k].get("result")) for k in common_keys) if v is not None]
        r_tokens = [v for v in (_online_tokens(right_map[k].get("result")) for k in common_keys) if v is not None]
        l_mean_tools = sum(l_tools) / len(l_tools) if l_tools else None
        r_mean_tools = sum(r_tools) / len(r_tools) if r_tools else None
        l_mean_tokens = sum(l_tokens) / len(l_tokens) if l_tokens and len(l_tokens) == len(common_keys) else (sum(l_tokens) / len(l_tokens) if l_tokens else None)
        r_mean_tokens = sum(r_tokens) / len(r_tokens) if r_tokens and len(r_tokens) == len(common_keys) else (sum(r_tokens) / len(r_tokens) if r_tokens else None)
        paired_solved_cost[f"{left_cond}_vs_{right_cond}"] = {
            "left_condition": left_cond,
            "right_condition": right_cond,
            "common_solved_count": len(common_keys),
            "common_tasks": [f"{repo}:{stage}" for repo, stage in common_keys],
            "left_mean_tool_calls": l_mean_tools,
            "right_mean_tool_calls": r_mean_tools,
            "delta_tool_calls": (r_mean_tools - l_mean_tools) if (l_mean_tools is not None and r_mean_tools is not None) else None,
            "left_mean_online_tokens": l_mean_tokens,
            "right_mean_online_tokens": r_mean_tokens,
            "delta_online_tokens": (r_mean_tokens - l_mean_tokens) if (l_mean_tokens is not None and r_mean_tokens is not None) else None,
        }

    comparisons = {
        "a2_minus_a1_solve_rate": _rate_delta("A2", "A1"),
        "a2_minus_a1_transfer_solve_rate": _rate_delta("A2", "A1", "transfer"),
        "a2_minus_a1_stale_solve_rate": _rate_delta("A2", "A1", "stale"),
        "a2_minus_a1_heldout_solve_rate": _rate_delta("A2", "A1", "heldout"),
        "b3_minus_b2_solve_rate": _rate_delta("B3", "B2"),
        "b4_minus_b3_solve_rate": _rate_delta("B4", "B3"),
        "b4_minus_b2_solve_rate": _rate_delta("B4", "B2"),
        "nsrr_value": nsrr.get("value"),
        "nsrr_status": nsrr.get("status"),
    }
    return {
        "by_condition": by_condition,
        "skill_transfer": skill_transfer,
        "paired_solved_cost": paired_solved_cost,
        "comparisons": comparisons,
    }


def render_formal_markdown_report(summary: dict, all_rows: list[dict], metrics: dict, nsrr: dict, stop: dict | None) -> list[str]:
    """生成包含分组、分阶段、迁移率与配对成本对比的正式 Markdown 报告。"""
    def _fmt_pct(val: float | None) -> str:
        return f"{val:.1%}" if isinstance(val, (int, float)) else "N/A"

    def _fmt_num(val: float | int | None) -> str:
        if val is None:
            return "N/A"
        return f"{val:.1f}" if isinstance(val, float) else str(val)

    lines = [
        "# EvoCode Benchmark formal run",
        "",
        "Formal results are generated only from recorded task attempts and independent grading.",
        "",
        f"Planned: 54; recorded: {summary['recorded']}; verified grades: {summary['verified']}; unverified: {summary['unverified']}; coverage: {summary['coverage']:.1%}.",
        f"Solved: {summary['solved']}; overall solve rate: {_fmt_pct(summary['solve_rate'])}; successes / planned: {summary['successes_over_planned']:.1%}.",
        f"NSRR: {nsrr.get('status')}; K/N: {nsrr.get('value') if nsrr.get('value') is not None else 'N/A'}.",
        "",
        "## 1. Condition Summary (五大核心指标分组汇总)",
        "",
        "| Condition | Planned | Verified | Unverified | Solved | Solve Rate | Tool Calls / Solved | Online Tokens / Solved | Learning Tokens | Total Tokens | Regression Rate | Published / Proposed |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cond_name, row in metrics["by_condition"].items():
        learn = row["learning"]
        lines.append(
            f"| {cond_name} | {row['planned']} | {row['verified']} | {row['unverified']} | {row['solved']} | "
            f"{_fmt_pct(row['solve_rate'])} | {_fmt_num(row['tool_calls_per_solved'])} | {_fmt_num(row['tokens_per_solved'])} | "
            f"{_fmt_num(learn['total_learning_tokens'])} | {_fmt_num(row['total_cost']['total_tokens'])} | "
            f"{_fmt_pct(row['regression_rate'])} | {learn['candidates_published']}/{learn['candidates_proposed']} |"
        )

    lines.extend([
        "",
        "## 2. Stage Solve Rate Breakdown (分阶段成功率)",
        "",
        "| Condition | T1 (seed) | T2 (transfer) | T3 (stale) | T4 (heldout) | Overall |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for cond_name, row in metrics["by_condition"].items():
        stages = row["by_stage"]
        s_cells = []
        for s_name in ("seed", "transfer", "stale", "heldout"):
            if s_name not in stages:
                s_cells.append("—")
            else:
                st = stages[s_name]
                s_cells.append(f"{st['solved']}/{st['verified']} ({_fmt_pct(st['solve_rate'])})")
        lines.append(
            f"| {cond_name} | {s_cells[0]} | {s_cells[1]} | {s_cells[2]} | {s_cells[3]} | "
            f"{row['solved']}/{row['verified']} ({_fmt_pct(row['solve_rate'])}) |"
        )

    lines.extend([
        "",
        "## 3. Skill Transfer & Post-Meta Summary (B2 / B3 / B4 迁移与元进化对比)",
        "",
        "| Condition | T2 Transfer Solve Rate | T4 Heldout Solve Rate | Combined (T2+T4) Solve Rate | Post-Meta (T3+T4) Solve Rate |",
        "|---|---:|---:|---:|---:|",
    ])
    for cond_name, tr in metrics["skill_transfer"]["by_condition"].items():
        lines.append(
            f"| {cond_name} | {_fmt_pct(tr['t2_transfer']['solve_rate'])} | {_fmt_pct(tr['t4_heldout']['solve_rate'])} | "
            f"{_fmt_pct(tr['combined_t2_t4']['solve_rate'])} | {_fmt_pct(tr['post_meta_t3_t4']['solve_rate'])} |"
        )

    lines.extend([
        "",
        "## 4. Paired-Solved Cost (共同成功题目配对成本)",
        "",
        "| Pair | Common Solved Tasks | Left Tool Calls | Right Tool Calls | Delta Tool Calls | Left Online Tokens | Right Online Tokens | Delta Online Tokens |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for pair_key, p_row in metrics["paired_solved_cost"].items():
        lines.append(
            f"| {pair_key} | {p_row['common_solved_count']} | {_fmt_num(p_row['left_mean_tool_calls'])} | "
            f"{_fmt_num(p_row['right_mean_tool_calls'])} | {_fmt_num(p_row['delta_tool_calls'])} | "
            f"{_fmt_num(p_row['left_mean_online_tokens'])} | {_fmt_num(p_row['right_mean_online_tokens'])} | "
            f"{_fmt_num(p_row['delta_online_tokens'])} |"
        )

    lines.extend([
        "",
        "## 5. Position Ledger (54 位置明细)",
        "",
        "| # | Condition | Repository | Stage | State | Grade | Solved |",
        "|---:|---|---|---|---|---|---|",
    ])
    lines.extend(
        f"| {row['position']} | {row['condition']} | {row['repository_id']} | {row['stage']} | {row['attempt_state']} | {row['grade_status'] or 'missing'} | {row['solved']} |"
        for row in all_rows
    )
    lines += ["", f"Stop reason: {stop['reason'] if stop else 'none'}.", ""]
    return lines


async def _return_fixed_meta_status():
    return {"status": "fixed_v1", "reviewed": False}
