"""Black-box Raw-RAG evaluation over independent public EvoCode instances.

The runner intentionally delegates task creation, execution, persistence snapshots,
and acceptance checks to ``blackbox_runtime``. It never calls MemoryService or
creates a search probe. A pair is a protocol-level ON/OFF comparison of identical
warmup and held-out tasks with independent state snapshots.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
CONDITIONS = ("ON", "OFF")


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def repository_id(path: Path) -> str:
    """Stable identity used by the public TaskSpec repository_id field."""
    return hashlib.sha256(str(path.expanduser().resolve()).encode("utf-8")).hexdigest()[:24]


def read_manifest(path: Path) -> tuple[dict, Path, list[dict]]:
    manifest_path = path.expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tasks"), list):
        raise ValueError("Raw-RAG manifest must contain a tasks array")
    root = manifest_path.parent
    tasks = []
    seen = set()
    for raw in manifest["tasks"]:
        task = dict(raw)
        task_id = task.get("task_id")
        if not task_id or task_id in seen:
            raise ValueError("Raw-RAG task IDs must be non-empty and unique")
        seen.add(task_id)
        for field in ("task_spec", "workspace", "training_workspace", "acceptance_file"):
            value = task.get(field)
            if value:
                resolved = (root / value).resolve()
                if not resolved.is_relative_to(root):
                    raise ValueError(f"{field} must stay inside the manifest directory: {task_id}")
                task[f"_{field}"] = resolved
        spec = json.loads(task["_task_spec"].read_text(encoding="utf-8"))
        if not spec.get("prompt") or not task["_workspace"].is_dir() or not task["_training_workspace"].is_dir():
            raise ValueError(f"Raw-RAG fixture is incomplete: {task_id}")
        task["_spec"] = spec
        task["_acceptance_source"] = task["_acceptance_file"].read_text(encoding="utf-8")
        if not task.get("training_prompt"):
            raise ValueError(f"Raw-RAG fixture has no training prompt: {task_id}")
        if not task.get("training_acceptance_command"):
            raise ValueError(f"Raw-RAG fixture has no training acceptance command: {task_id}")
        tasks.append(task)
    expected = manifest.get("task_count")
    if expected is not None and expected != len(tasks):
        raise ValueError(f"manifest task_count={expected} but listed {len(tasks)} tasks")
    return manifest, root, tasks


def selected_tasks(tasks: list[dict], task_id: str | None) -> list[dict]:
    if task_id is None:
        return tasks
    selected = [task for task in tasks if task["task_id"] == task_id]
    if not selected:
        raise ValueError(f"unknown Raw-RAG task_id: {task_id}")
    return selected


def _outcome(status: str | None, acceptance: dict | None, error: str | None = None) -> dict:
    acceptance = acceptance or {}
    protected = bool(acceptance.get("acceptance_protected"))
    passed = bool(acceptance.get("passed"))
    completed = status == "completed"
    infra_failed = bool(error)
    # Readiness failures (for example no retrieval) do not invalidate an oracle
    # result. Only a missing run/oracle means this outcome cannot be scored.
    if infra_failed or status is None:
        score = "unscored_infrastructure"
    elif not completed:
        score = "noncompletion"
    elif not protected:
        score = "unscored_oracle_unavailable"
    else:
        score = "pass" if passed else "fail"
    return {
        "agent_terminal_status": status or "not_returned",
        "agent_execution_completed": completed,
        "tests_protected": protected,
        "protected_tests_passed": passed,
        "task_passed": score == "pass",
        "scored_outcome": score,
        "outcome_scoreable": score in {"pass", "fail", "noncompletion"},
        "evaluation_infrastructure_failure": infra_failed or status is None or (completed and not protected),
        "acceptance_outcome": "unverified" if not protected else "passed" if passed else "failed",
        "failure_reason": error or acceptance.get("acceptance_output", "")[-1200:] or None,
    }


def _retrieval_rows(telemetry: dict | None) -> list[dict]:
    rows = []
    for raw in (telemetry or {}).get("retrievals", []) or []:
        row = dict(raw)
        result = row.get("result")
        if isinstance(result, dict):
            for key in ("status", "mode", "reason", "fts_hits", "dense_hits", "reranked"):
                row.setdefault(key, result.get(key))
        rows.append(row)
    return rows


def _injection_proofs(telemetry: dict | None) -> list[dict]:
    """Keep only assembled evidence contexts linked to a successful model usage."""
    telemetry = telemetry or {}
    proofs = []
    for context in telemetry.get("evidence_contexts", []) or []:
        evidence_refs = sorted(set(context.get("evidence_refs") or [])
                               | set(context.get("tool_evidence_refs") or []))
        if context.get("context_id") and evidence_refs:
            proofs.append({
                "role": context.get("role"),
                "context_id": context["context_id"],
                "evidence_refs": evidence_refs,
                "context_evidence_refs": context.get("evidence_refs") or [],
                "tool_evidence_refs": context.get("tool_evidence_refs") or [],
                "skill_refs": context.get("skill_refs") or [],
                "observed_model_request": True,
                "proof_basis": "context_id linked to completed model.usage event",
            })
    return proofs


def _condition_health(condition: str, task_result: dict | None, error: str | None) -> dict:
    task_result = task_result or {}
    telemetry = task_result.get("telemetry") or {}
    retrievals = _retrieval_rows(telemetry)
    proofs = _injection_proofs(telemetry)
    if condition == "OFF":
        isolation_ready = not proofs and all(
            row.get("status") in {"memory_disabled", "disabled"} for row in retrievals
        )
    else:
        isolation_ready = bool(retrievals) and all(
            row.get("mode") == "hybrid"
            and row.get("status") in {"ok", "empty"}
            and (not (row.get("fts_hits") or row.get("dense_hits")) or row.get("reranked"))
            for row in retrievals
        )
    hybrid_ready = condition == "OFF" or bool(retrievals) and all(
        row.get("mode") == "hybrid" and row.get("status") in {"ok", "empty"}
        and (not (row.get("fts_hits") or row.get("dense_hits")) or row.get("reranked"))
        for row in retrievals
    )
    exposure_ready = not proofs if condition == "OFF" else bool(proofs)
    usage_ready = telemetry.get("tokens_complete") is True
    return {
        "condition": condition,
        "task_id": task_result.get("task_id"),
        "agent_terminal_status": task_result.get("status"),
        "model_calls": telemetry.get("model_calls"),
        "reported_tokens": telemetry.get("reported_tokens"),
        "tokens_complete": telemetry.get("tokens_complete"),
        "retrievals": retrievals,
        "evidence_contexts": telemetry.get("evidence_contexts", []),
        "skill_contexts": telemetry.get("skill_contexts", []),
        "evidence_injection_proofs": proofs,
        "rawrag_condition_clear": isolation_ready,
        "rawrag_condition_ready": isolation_ready,
        "readiness_diagnostics": {
            "hybrid_retrieval_ready": hybrid_ready,
            "evidence_exposure_ready": exposure_ready,
            "token_usage_complete": usage_ready,
            "retrieval_observed": bool(retrievals),
            "evidence_exposure_observed": bool(proofs),
        },
        "branch_returned": bool(task_result),
        "branch_error": error,
    }


def _pair_scoreable(on: dict, off: dict, *, aligned: bool) -> bool:
    """Completed tasks need a verified oracle; known noncompletion does not."""
    if not aligned or not on.get("outcome_scoreable") or not off.get("outcome_scoreable"):
        return False
    planned = on.get("planned_acceptance_sha256")
    if not planned or planned != off.get("planned_acceptance_sha256"):
        return False
    return all(row.get("scored_outcome") == "noncompletion" or row.get("acceptance_aligned")
               for row in (on, off))


def _score_summary(records: list[dict]) -> dict:
    per_condition = {}
    for condition in CONDITIONS:
        branches = [branch for pair in records for branch in pair.get("records", [])
                    if branch.get("condition") == condition]
        outcomes = [branch.get("scored_outcome") for branch in branches]
        per_condition[condition] = {
            "planned": len(records),
            "scored": sum(bool(branch.get("outcome_scoreable")) for branch in branches),
            "pass": outcomes.count("pass"),
            "fail": outcomes.count("fail"),
            "noncompletion": outcomes.count("noncompletion"),
            "unscored": sum(outcome not in {"pass", "fail", "noncompletion"} for outcome in outcomes),
        }
    eligible = [pair for pair in records if pair.get("eligible_for_comparison")]
    on_wins = off_wins = ties = 0
    for pair in eligible:
        branches = {branch.get("condition"): branch for branch in pair.get("records", [])}
        on_passed = branches.get("ON", {}).get("scored_outcome") == "pass"
        off_passed = branches.get("OFF", {}).get("scored_outcome") == "pass"
        if on_passed and not off_passed:
            on_wins += 1
        elif off_passed and not on_passed:
            off_wins += 1
        else:
            ties += 1
    return {
        "conditions": per_condition,
        "paired_wins": {
            "eligible_pairs": len(eligible), "ON_wins": on_wins,
            "OFF_wins": off_wins, "ties": ties,
            "win_definition": "one condition passes its protected oracle and the other does not; all other eligible pairs tie",
        },
    }


async def _memory_index_snapshot(instance, condition: str) -> dict:
    """Read index status once for OFF; only ON waits for background indexing."""
    memory_status = await instance.request("GET", "/api/memory")
    if condition == "OFF":
        return memory_status
    deadline = time.monotonic() + instance.timeout
    while True:
        jobs = memory_status.get("jobs", {})
        if int(jobs.get("failed", 0) or 0) or not any(
            int(jobs.get(key, 0) or 0) for key in ("pending", "processing")
        ) or time.monotonic() >= deadline:
            return memory_status
        await asyncio.sleep(0.25)
        memory_status = await instance.request("GET", "/api/memory")


async def _bootstrap_condition(runtime, condition: str, tasks: list[dict], root: Path, base_config,
                               collection_name: str) -> dict:
    config = base_config.model_copy(deep=True)
    config.data_dir = str(root / f"warmup-{condition.lower()}-state")
    config.raw_rag.collection = collection_name
    config.raw_rag.enabled = condition == "ON"
    config.raw_rag.index_when_disabled = False
    config.raw_rag.background_indexing = condition == "ON"
    config.evolution.enabled = False
    config.skills.enabled = False
    run_rows = []
    async with runtime.Instance(config) as instance:
        for task in tasks:
            work = root / f"warmup-{condition.lower()}-{task['task_id']}"
            shutil.copytree(task["_training_workspace"], work)
            held_repo_id = repository_id(task["_workspace"])
            source_repo_id = repository_id(task["_training_workspace"])
            mapped = task.get("training_scope") == "same_project_mapped"
            effective_repo_id = held_repo_id if mapped else source_repo_id
            try:
                check_relative = Path(task["training_acceptance_command"][-1])
                training_check = (task["_training_workspace"] / check_relative).resolve()
                if not training_check.is_relative_to(task["_training_workspace"]):
                    raise ValueError("training acceptance command must point inside training_workspace")
                check_source = training_check.read_text(encoding="utf-8")
                editable_check = work / check_relative
                if editable_check.exists() or editable_check.is_symlink():
                    editable_check.unlink()
                result = await instance.run_task(work, task["training_prompt"], effective_repo_id)
                acceptance = await runtime.protected_acceptance(
                    config, work, root / f"oracle-warmup-{condition.lower()}" / task["task_id"],
                    check_relative.as_posix(), check_source, task["training_acceptance_command"],
                )
                run_rows.append({
                    "task_id": task["task_id"], "status": result.get("status"),
                    "source_repository_id": source_repo_id,
                    "effective_repository_id": effective_repo_id,
                    "held_out_repository_id": held_repo_id,
                    "repository_scope_mapping": "same_project_mapped" if mapped else "foreign_scope_unmapped",
                    "acceptance": acceptance,
                    "telemetry": result.get("telemetry", {}),
                    "error": None,
                })
            except Exception as exc:  # keep a failed warmup in the result denominator
                run_rows.append({
                    "task_id": task["task_id"], "status": "failed",
                    "source_repository_id": source_repo_id,
                    "effective_repository_id": effective_repo_id,
                    "held_out_repository_id": held_repo_id,
                    "repository_scope_mapping": "same_project_mapped" if mapped else "foreign_scope_unmapped",
                    "acceptance": None, "telemetry": {},
                    "error": f"{type(exc).__name__}: {exc}",
                })
        memory_status = await _memory_index_snapshot(instance, condition)
        snapshot = root / f"warmup-{condition.lower()}-snapshot"
        await instance.snapshot(snapshot)
        await instance.wait_idle()
    pending = sum(int(memory_status.get("jobs", {}).get(key, 0) or 0)
                  for key in ("pending", "processing"))
    failed = int(memory_status.get("jobs", {}).get("failed", 0) or 0)
    index_healthy = condition == "OFF" or (pending == 0 and failed == 0)
    return {
        "condition": condition,
        "snapshot": snapshot,
        "snapshot_sha256": _snapshot_hash(snapshot),
        "warmups": run_rows,
        "warmup_failures": [row["task_id"] for row in run_rows
                            if row.get("status") != "completed"
                            or not (row.get("acceptance") or {}).get("acceptance_protected")
                            or not (row.get("acceptance") or {}).get("passed")],
        "memory_index_status": memory_status,
        "index_healthy": index_healthy,
    }


def _snapshot_hash(snapshot: Path) -> str:
    if snapshot.is_file():
        return hashlib.sha256(snapshot.read_bytes()).hexdigest()
    return tree_hash(snapshot)


async def _run_branch(runtime, condition: str, task: dict, branch_root: Path, warmup: dict,
                      base_config, collection_name: str, held_hashes: dict) -> dict:
    config = base_config.model_copy(deep=True)
    config.data_dir = str(branch_root / "state")
    config.raw_rag.collection = collection_name
    config.raw_rag.enabled = condition == "ON"
    config.raw_rag.index_when_disabled = False
    config.raw_rag.background_indexing = False  # holdout snapshot is read-only for Raw-RAG
    config.evolution.enabled = False
    config.skills.enabled = False
    state_copy = branch_root / "state"
    runtime.clone_state(warmup["snapshot"], state_copy)
    held_workspace = branch_root / "workspace"
    shutil.copytree(task["_workspace"], held_workspace)
    held_repo_id = repository_id(task["_workspace"])
    task_spec_hash = canonical_hash({
        "prompt": task["_spec"]["prompt"],
        "acceptance_command": task["_spec"].get("acceptance_command", []),
    })
    workspace_hash = tree_hash(task["_workspace"])
    config_for_hash = config.snapshot()
    config_for_hash["raw_rag"]["enabled"] = True
    config_for_hash.pop("data_dir", None)
    task_result = None
    acceptance = None
    execution_error = None
    oracle_error = None
    noncompletion_error = None
    try:
        async with runtime.Instance(config) as instance:
            try:
                task_result = await instance.run_task(held_workspace, task["_spec"]["prompt"], held_repo_id)
            except Exception as exc:
                timeout_type = getattr(runtime, "TaskExecutionTimedOut", None)
                if timeout_type is not None and isinstance(exc, timeout_type):
                    task_result = {"status": "timed_out", "telemetry": {
                        "retrievals": [], "evidence_contexts": [], "skill_contexts": [],
                        "tokens_complete": False, "model_calls": None, "reported_tokens": None,
                    }}
                    noncompletion_error = f"{type(exc).__name__}: {exc}"
                else:
                    execution_error = f"{type(exc).__name__}: {exc}"
            if (task_result or {}).get("status") == "completed":
                try:
                    acceptance = await runtime.protected_acceptance(
                        config, held_workspace, branch_root / "oracle", "tests/prevalidation_acceptance.py",
                        task["_acceptance_source"], task["_spec"].get("acceptance_command", []),
                    )
                except Exception as exc:
                    oracle_error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # hold onto each branch failure rather than dropping its pair
        execution_error = execution_error or f"{type(exc).__name__}: {exc}"
    if (acceptance or {}).get("acceptance_protected") and acceptance.get("acceptance_file_sha256") != held_hashes:
        oracle_error = "Protected oracle file hash did not match the planned acceptance file"
    error = execution_error or oracle_error
    health = _condition_health(condition, task_result, error)
    acceptance_aligned = bool(acceptance and acceptance.get("acceptance_protected")
                              and acceptance.get("acceptance_file_sha256") == held_hashes)
    return {
        **health,
        "task_spec_sha256": task_spec_hash,
        "workspace_template_sha256": workspace_hash,
        "config_sha256": canonical_hash(config_for_hash),
        "repository_id": held_repo_id,
        "acceptance": acceptance,
        "acceptance_file_sha256": (acceptance or {}).get("acceptance_file_sha256", {}),
        "acceptance_aligned": acceptance_aligned,
        "planned_acceptance_sha256": held_hashes.get("tests/prevalidation_acceptance.py"),
        "protected_oracle_verified": acceptance_aligned,
        "protected_oracle_skipped_for_noncompletion": bool(
            task_result and (task_result.get("status") != "completed")),
        **_outcome((task_result or {}).get("status"), acceptance, error),
        "warmup_snapshot_sha256": warmup["snapshot_sha256"],
        "warmup_failures": warmup["warmup_failures"],
        "memory_writes_frozen": True,
        "execution_infrastructure_error": execution_error,
        "oracle_infrastructure_error": oracle_error,
        "agent_noncompletion_reason": noncompletion_error,
    }


async def execute(args) -> dict:
    import blackbox_runtime as runtime

    manifest, manifest_root, all_tasks = read_manifest(args.manifest)
    tasks = selected_tasks(all_tasks, args.task_id)
    config = runtime.load_live_config()
    if args.max_turns < 1:
        raise ValueError("--max-turns must be positive")
    config.runtime.max_turns = args.max_turns
    if not config.raw_rag.embedding_model or not config.raw_rag.rerank_enabled or not config.raw_rag.rerank_model:
        raise ValueError("Raw-RAG execution requires configured embedding and reranker providers")
    config.raw_rag.index_when_disabled = False
    config.raw_rag.background_indexing = True
    with tempfile.TemporaryDirectory(prefix="rawrag-blackbox-") as directory:
        root = Path(directory)
        rows = []
        async with runtime.isolated_collection(config.raw_rag) as collection_name:
            warmups = {}
            # Independent service instances and persisted histories per condition.
            for condition in CONDITIONS:
                try:
                    warmups[condition] = await _bootstrap_condition(
                        runtime, condition, tasks, root, config, collection_name,
                    )
                except Exception as exc:
                    # A failed bootstrap is a condition-level infrastructure gap.
                    # Keep executing the other condition and retain every planned
                    # held-out pair in the report.
                    warmups[condition] = {
                        "condition": condition, "snapshot": None, "snapshot_sha256": None,
                        "warmups": [], "warmup_failures": [task["task_id"] for task in tasks],
                        "index_healthy": False,
                        "infrastructure_error": f"{type(exc).__name__}: {exc}",
                    }
            for task in tasks:
                order = list(CONDITIONS)
                random.Random(args.seed + sum(task["task_id"].encode("utf-8"))).shuffle(order)
                branches = {}
                held_hashes = {"tests/prevalidation_acceptance.py": hashlib.sha256(
                    task["_acceptance_source"].encode("utf-8")).hexdigest()}
                for condition in order:
                    branch_root = root / f"heldout-{task['task_id']}-{condition.lower()}"
                    try:
                        if not warmups[condition].get("snapshot"):
                            raise RuntimeError("condition warmup snapshot unavailable: "
                                               + str(warmups[condition].get("infrastructure_error")))
                        branches[condition] = await _run_branch(
                            runtime, condition, task, branch_root, warmups[condition], config,
                            collection_name, held_hashes,
                        )
                    except Exception as exc:  # clone/start failures also count as outcomes
                        branches[condition] = {
                            "condition": condition, "branch_returned": False,
                            "branch_error": f"{type(exc).__name__}: {exc}",
                            "agent_terminal_status": "not_returned", "task_passed": False,
                            "scored_outcome": "unscored_infrastructure",
                            "outcome_scoreable": False,
                            "evaluation_infrastructure_failure": True,
                            "acceptance_outcome": "unverified", "rawrag_condition_clear": False,
                            "planned_acceptance_sha256": held_hashes["tests/prevalidation_acceptance.py"],
                            "protected_oracle_verified": False,
                            "protected_oracle_skipped_for_noncompletion": False,
                            "rawrag_condition_ready": False,
                            "readiness_diagnostics": {"hybrid_retrieval_ready": False,
                                                       "evidence_exposure_ready": False,
                                                       "token_usage_complete": False,
                                                       "retrieval_observed": False,
                                                       "evidence_exposure_observed": False},
                            "retrievals": [], "evidence_contexts": [], "skill_contexts": [],
                            "evidence_injection_proofs": [], "acceptance_aligned": False,
                            "warmup_failures": warmups[condition]["warmup_failures"],
                        }
                on, off = branches["ON"], branches["OFF"]
                aligned = all(on.get(key) and on.get(key) == off.get(key)
                              for key in ("task_spec_sha256", "config_sha256",
                                          "workspace_template_sha256", "repository_id"))
                acceptance_aligned = all(row.get("acceptance_aligned") for row in branches.values())
                hybrid_ok = on.get("rawrag_condition_clear") and any(
                    row.get("mode") == "hybrid" for row in on.get("retrievals", [])
                )
                wrong_scope = task.get("training_scope") == "foreign_scope_unmapped"
                if wrong_scope:
                    hybrid_ok = bool(hybrid_ok and not on.get("evidence_injection_proofs")
                                      and not any(row.get("fts_hits") or row.get("dense_hits")
                                                  for row in on.get("retrievals", [])))
                pair_complete = _pair_scoreable(on, off, aligned=aligned)
                planned_acceptance_aligned = bool(
                    on.get("planned_acceptance_sha256")
                    and on.get("planned_acceptance_sha256") == off.get("planned_acceptance_sha256")
                )
                readiness = {condition: branches[condition].get("readiness_diagnostics", {})
                             for condition in CONDITIONS}
                rows.append({
                    "task_id": task["task_id"],
                    "status": "complete" if pair_complete else "invalid/incomplete",
                    "pair_complete": pair_complete,
                    "condition_order": order,
                    "task_spec_sha256": on.get("task_spec_sha256") or off.get("task_spec_sha256"),
                    "workspace_template_sha256": on.get("workspace_template_sha256") or off.get("workspace_template_sha256"),
                    "repository_id": on.get("repository_id") or off.get("repository_id"),
                    "repository_scope_expectation": task.get("training_scope"),
                    "paired_task_alignment": aligned,
                    "protected_acceptance_aligned": acceptance_aligned,
                    "planned_acceptance_aligned": planned_acceptance_aligned,
                    "hybrid_passed": bool(hybrid_ok),
                    "eligible_for_comparison": pair_complete,
                    "oracle_requirements_met_for_score": pair_complete,
                    "pair_readiness_diagnostics": readiness,
                    "readiness_does_not_gate_scoring": True,
                    "failure_denominator": {
                        "expected": 2,
                        "attempted": sum(bool(branch.get("branch_returned")) for branch in branches.values()),
                        "agent_noncompletion": [key for key, branch in branches.items()
                                                if branch.get("agent_terminal_status") not in {"completed", "not_returned"}],
                        "infrastructure_failure_or_missing": [key for key, branch in branches.items()
                                                              if branch.get("evaluation_infrastructure_failure")],
                        "failed": [key for key, branch in branches.items()
                                   if branch.get("scored_outcome") in {"fail", "noncompletion"}],
                        "missing": [key for key in CONDITIONS if key not in branches],
                    },
                    "records": [branches[key] for key in CONDITIONS],
                })
        completed = sum(row["pair_complete"] for row in rows)
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "complete" if completed == len(rows) else "invalid/incomplete",
            "mode": "public_api_black_box",
            "manifest": str(args.manifest.expanduser().resolve()),
            "manifest_name": manifest.get("name"),
            "task_count": len(rows),
            "planned_pairs": len(rows),
            "scored_pairs": completed,
            "completed_pairs": completed,
            "conditions": list(CONDITIONS),
            "seed": args.seed,
            "max_turns_per_task": args.max_turns,
            "execution": {
                "task_submission": "POST /api/tasks through blackbox_runtime.Instance",
                "memory_query": "agent context flow only; no forced search/probe",
                "state_snapshots": {condition: warmups[condition]["snapshot_sha256"] for condition in CONDITIONS},
                "warmups": {condition: warmups[condition]["warmups"] for condition in CONDITIONS},
                "warmup_readiness": {condition: {
                    "index_healthy": warmups[condition].get("index_healthy", False),
                    "memory_index_status": warmups[condition].get("memory_index_status"),
                    "warmup_failures": warmups[condition].get("warmup_failures", []),
                    "infrastructure_error": warmups[condition].get("infrastructure_error"),
                } for condition in CONDITIONS},
                "qdrant_collection": collection_name,
                "temporary_directory_removed": True,
            },
            "failure_denominator": {
                "expected_pairs": len(rows), "completed_pairs": completed,
                "incomplete_task_ids": [row["task_id"] for row in rows if not row["pair_complete"]],
            },
            **_score_summary(rows),
            "records": rows,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path,
                        default=Path("tests/fixtures/prevalidation/rawrag/manifest.json"))
    parser.add_argument("--task-id", help="run one fixture pair for a bounded integration trial")
    parser.add_argument("--case", dest="task_id", help="alias for --task-id, e.g. convention_reuse")
    parser.add_argument("--execute", action="store_true", help="submit tasks to configured EvoCode HTTP instances")
    parser.add_argument("--max-turns", type=int, default=6,
                        help="bounded runtime turn budget applied equally to both conditions")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--output", type=Path, help="optional JSON result destination")
    return parser


def preflight(args) -> dict:
    manifest, _, tasks = read_manifest(args.manifest)
    tasks = selected_tasks(tasks, args.task_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "preflight",
        "mode": "public_api_black_box",
        "manifest": str(args.manifest.expanduser().resolve()),
        "task_count": len(tasks),
        "conditions": list(CONDITIONS),
        "max_turns_per_task": args.max_turns,
        "hybrid_passed": False,
        "note": "Preflight makes no HTTP requests and submits no model tasks. Add --execute to run the public API protocol.",
    }


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.execute:
        try:
            report = asyncio.run(execute(args))
        except Exception as exc:  # execution setup failures remain explicit in the denominator
            _, _, tasks = read_manifest(args.manifest)
            tasks = selected_tasks(tasks, args.task_id)
            setup_error = f"{type(exc).__name__}: {exc}"
            records = []
            for task in tasks:
                branches = [{
                    "condition": condition, "branch_returned": False,
                    "agent_terminal_status": "not_returned",
                    "scored_outcome": "unscored_infrastructure", "outcome_scoreable": False,
                    "evaluation_infrastructure_failure": True,
                    "branch_error": setup_error, "acceptance_outcome": "unverified",
                    "acceptance_aligned": False, "rawrag_condition_clear": False,
                    "planned_acceptance_sha256": hashlib.sha256(
                        task["_acceptance_source"].encode("utf-8")).hexdigest(),
                    "protected_oracle_verified": False,
                    "protected_oracle_skipped_for_noncompletion": False,
                    "readiness_diagnostics": {"hybrid_retrieval_ready": False,
                                               "evidence_exposure_ready": False,
                                               "token_usage_complete": False,
                                               "retrieval_observed": False,
                                               "evidence_exposure_observed": False},
                } for condition in CONDITIONS]
                records.append({
                    "task_id": task["task_id"], "status": "invalid/incomplete",
                    "pair_complete": False, "eligible_for_comparison": False,
                    "protected_acceptance_aligned": False,
                    "planned_acceptance_aligned": True,
                    "oracle_requirements_met_for_score": False,
                    "readiness_does_not_gate_scoring": True,
                    "failure_denominator": {
                        "expected": 2, "attempted": 0,
                        "agent_noncompletion": [],
                        "infrastructure_failure_or_missing": list(CONDITIONS),
                        "failed": [], "missing": list(CONDITIONS),
                    },
                    "records": branches,
                })
            report = {
                "schema_version": SCHEMA_VERSION, "status": "invalid/incomplete",
                "mode": "public_api_black_box", "manifest": str(args.manifest.expanduser().resolve()),
                "task_count": len(tasks), "planned_pairs": len(tasks),
                "scored_pairs": 0, "completed_pairs": 0,
                "execution_error": setup_error,
                "execution_error_scope": "setup or unhandled execution failure; no provider rerun attempted",
                "planned_pairs_retained": True,
                "failure_denominator": {"expected_pairs": len(tasks), "completed_pairs": 0,
                                        "incomplete_task_ids": [task["task_id"] for task in tasks]},
                **_score_summary(records),
                "records": records,
            }
    else:
        report = preflight(args)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report.get("status") in {"preflight", "complete"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
