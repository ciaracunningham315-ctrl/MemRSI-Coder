"""Resume only unfinished RSI held-out rows from a retained live run.

This keeps the original training and Evolution costs intact while completing a
paired held-out matrix after a live token budget has been explicitly raised.
The parent report is never overwritten.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import blackbox_runtime, live_rsi_prevalidation as evaluation
from evocode.token_budget import TokenBudgetLedger, TokenBudgetExceeded

BRANCHES = {
    "no_learning": {"freeze_meta_skill": True},
    "task_skill_only": {"freeze_meta_skill": True},
    "full_rsi": {"freeze_meta_skill": False},
}


def atomic_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def backup_sqlite(source: Path, backup: Path) -> None:
    backup.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as source_conn, closing(sqlite3.connect(backup)) as backup_conn:
        source_conn.backup(backup_conn)
    backup.chmod(0o600)


def verify_snapshot(path: Path, parent: dict, branch: str) -> dict:
    """Check snapshot skill/meta state against the parent run's recorded boundary."""
    database = path / "evocode.db"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        actual_versions = sorted(conn.execute("""SELECT sv.version_id,s.name,s.level,
            sv.content_sha256,sv.status,s.scope_id
            FROM skill_versions sv JOIN skills s USING(skill_id)""").fetchall())
        meta_rows = conn.execute("""SELECT s.active_version_id,sv.content_sha256
            FROM skills s LEFT JOIN skill_versions sv ON s.active_version_id=sv.version_id
            WHERE s.level='meta' AND s.active_version_id IS NOT NULL""").fetchall()
    expected_versions = sorted(tuple(item.get(key) for key in
        ("version_id", "name", "level", "content_sha256", "status", "scope_id"))
        for item in parent["phases"][branch]["phase_2"].get("skill_versions", []))
    if actual_versions != expected_versions:
        raise ValueError(f"Snapshot skill versions do not match recorded phase_2 boundary for {branch}")
    expected_meta = parent["phases"][branch]["phase_2"].get("active_meta_at_boundary") or {}
    expected_version = expected_meta.get("version_id")
    if expected_version is None:
        if meta_rows:
            raise ValueError(f"Snapshot has an active Meta Skill absent from parent boundary for {branch}")
    elif (len(meta_rows) != 1 or
          (meta_rows[0][0], meta_rows[0][1]) !=
          (expected_version, expected_meta.get("content_sha256"))):
        raise ValueError(f"Snapshot active Meta Skill differs from parent boundary for {branch}")
    return {"skill_version_count": len(actual_versions),
            "active_meta_version_id": expected_version,
            "matches_parent_phase_2_boundary": True}


def summarize_pairs(rows: list[dict], families: list[str],
                    expected_task_ids: list[str] | None = None) -> tuple[dict, dict, bool, int]:
    expected_task_ids = expected_task_ids or [
        f"{family}_{ordinal:02d}" for family in families for ordinal in (1, 2)]
    expected_keys = {(branch, task_id) for branch in BRANCHES for task_id in expected_task_ids}
    expected = len(expected_keys)
    pass_rates = {}
    for branch in BRANCHES:
        arm_rows = [row for row in rows if row.get("branch") == branch]
        pass_rates[branch] = {
            "attempted": sum(row.get("agent_call_started") is True for row in arm_rows),
            "solved": sum(row.get("solved") is True for row in arm_rows),
            "oracle_unverified": sum(row.get("oracle_class") == "unverified" for row in arm_rows),
            "oracle_assertion_failures": sum(row.get("oracle_class") == "assertion_fail" for row in arm_rows),
            "runner_failures": sum(row.get("oracle_class") == "runner_failure" for row in arm_rows),
            "agent_timeouts": sum(row.get("oracle_class") == "agent_timeout" for row in arm_rows),
            "agent_noncompletions": sum(row.get("oracle_class") in {"agent_noncompletion", "agent_timeout"}
                                         for row in arm_rows),
        }
    task_ids = sorted({row.get("task_id") for row in rows})
    comparisons = {}
    branch_names = list(BRANCHES)
    for left_index, left_branch in enumerate(branch_names):
        for right_branch in branch_names[left_index + 1:]:
            pair = {f"{left_branch}_wins": 0, f"{right_branch}_wins": 0,
                    "ties": 0, "unscored_pairs": 0, "scored_pairs": 0}
            for task_id in task_ids:
                left = next((row for row in rows if row.get("branch") == left_branch and
                             row.get("task_id") == task_id), None)
                right = next((row for row in rows if row.get("branch") == right_branch and
                              row.get("task_id") == task_id), None)
                if (left is None or right is None or left.get("runner_failure") or right.get("runner_failure") or
                        left.get("oracle_class") == "unverified" or right.get("oracle_class") == "unverified"):
                    pair["unscored_pairs"] += 1
                    continue
                pair["scored_pairs"] += 1
                left_won, right_won = left.get("solved") is True, right.get("solved") is True
                if left_won == right_won:
                    pair["ties"] += 1
                else:
                    pair[f"{left_branch if left_won else right_branch}_wins"] += 1
            comparisons[f"{left_branch}_vs_{right_branch}"] = pair
    row_keys = {(row.get("branch"), row.get("task_id")) for row in rows}
    complete = (row_keys == expected_keys and len(rows) == expected and
                all(row.get("oracle_class") not in {"runner_failure", "unverified"}
                    for row in rows))
    runner_failures = sum(bool(row.get("runner_failure")) for row in rows)
    return pass_rates, comparisons, complete, runner_failures


def compute_mechanism_gates(report: dict, rows: list[dict]) -> tuple[dict, bool, bool]:
    phases = report.get("phases", {})
    lifecycle = report.get("lifecycle", {})
    gates = {}
    initial_snapshots = {}
    for branch in BRANCHES:
        phase1 = phases.get(branch, {}).get("phase_1", {})
        phase2 = phases.get(branch, {}).get("phase_2", {})
        life = lifecycle.get(branch, {})
        task_runs = [run for phase in (phase1, phase2) for run in phase.get("task_skill_runs", [])]
        meta_runs = [run for phase in (phase1, phase2) for run in phase.get("meta_runs", [])]
        links = [*(life.get("phase_1_boundary", {}).get("task_skill_links", []) or []),
                 *(life.get("phase_2_task_skill_links", []) or [])]
        life["all_task_skill_links"] = links
        arm_rows = [row for row in rows if row.get("branch") == branch]
        for row in arm_rows:
            loaded_by_phase = evaluation.loaded_task_skill_links_by_phase(
                row.get("task", {}).get("coder_skill_injection_evidence"),
                life.get("phase_1_boundary", {}).get("task_skill_links", []) or [],
                life.get("phase_2_task_skill_links", []) or [], family=row.get("family"))
            row["loaded_phase_1_task_skill_links"] = loaded_by_phase["phase_1"]
            row["loaded_phase_2_task_skill_links"] = loaded_by_phase["phase_2"]
            row["loaded_task_skill_links"] = [
                *loaded_by_phase["phase_1"], *loaded_by_phase["phase_2"]]
            row["used_published_task_skill"] = bool(row["loaded_task_skill_links"])
        heldout_skill_usage = {
            "loaded_phase_1_task_skill_links": sum(
                len(row.get("loaded_phase_1_task_skill_links", [])) for row in arm_rows),
            "loaded_phase_2_task_skill_links": sum(
                len(row.get("loaded_phase_2_task_skill_links", [])) for row in arm_rows),
            "total_loaded_published_task_skill_links": sum(
                len(row.get("loaded_task_skill_links", [])) for row in arm_rows),
            "heldout_rows_using_published_task_skill": sum(
                row.get("used_published_task_skill") is True for row in arm_rows),
        }
        loaded_by_family = {
            family: any(row.get("family") == family and any(
                item.get("family") == family for item in row.get("loaded_task_skill_links", [])
            ) for row in arm_rows)
            for family in report.get("families", [])
        }
        published_by_family = {
            family: any(link.get("family") == family and link.get("status") == "published" and
                        link.get("published_version") for link in links)
            for family in report.get("families", [])
        }
        injection_by_family = {
            family: [item for row in arm_rows if row.get("family") == family
                     for item in row.get("loaded_task_skill_links", [])
                     if item.get("family") == family and
                     item.get("injection_verified") is True and
                     item.get("model_call_observed") is True]
            for family in report.get("families", [])
        }
        activity = evaluation.lifecycle_activity(task_runs, meta_runs)
        initial = life.get("initial_active_meta") or {}
        final = phase2.get("active_meta_at_boundary") or {}
        meta_changed = ((initial.get("version_id"), initial.get("content_sha256")) !=
                        (final.get("version_id"), final.get("content_sha256")))
        initial_skill_versions = life.get("initial_skill_versions", [])
        final_skill_versions = phase2.get("skill_versions", [])
        initial_ids = {item.get("version_id") for item in initial_skill_versions}
        final_ids = {item.get("version_id") for item in final_skill_versions}
        initial_snapshots[branch] = sorted((item.get("version_id"), item.get("content_sha256"))
                                           for item in initial_skill_versions)
        if branch == "no_learning":
            new_versions = [item.get("version_id") for item in final_skill_versions
                            if item.get("level") == "task" and item.get("version_id") not in initial_ids]
            passed = (not life.get("thresholds", {}).get("evolution_enabled", True)
                      and not task_runs and not meta_runs and not (new_versions or (final_ids - initial_ids)))
            gates[branch] = {"passed": passed, "evolution_disabled": True,
                             "task_skill_runs": len(task_runs), "meta_skill_runs": len(meta_runs),
                             "new_task_skill_versions": new_versions,
                             "mechanism_proposal_and_evaluation_activity": activity,
                             "candidate_decisions": activity["candidate_decisions"]}
        elif branch == "task_skill_only":
            passed = (not meta_changed and any(run.get("status") == "published" for run in task_runs) and
                      all(published_by_family.values()) and all(loaded_by_family.values()))
            gates[branch] = {"passed": passed, "meta_unchanged": not meta_changed,
                             "published_task_skill_by_family": published_by_family,
                             "published_task_skill_loaded_in_heldout_by_family": loaded_by_family,
                             "heldout_published_task_skill_usage": heldout_skill_usage,
                             "task_skill_injection_evidence_by_family": injection_by_family,
                             "mechanism_proposal_and_evaluation_activity": activity,
                             "candidate_decisions": activity["candidate_decisions"],
                             "task_skill_runs": len(task_runs), "meta_skill_runs": len(meta_runs)}
        else:
            changed_meta_source_links = [link for link in links
                                         if link.get("source_meta_matches_active_before") is True and
                                         (link.get("source_meta_skill_version_id"),
                                          link.get("source_meta_content_sha256")) !=
                                         (initial.get("version_id"), initial.get("content_sha256"))]
            changed_meta_links = [link for link in changed_meta_source_links
                                  if link.get("status") == "published" and link.get("published_version")]
            loaded_changed = any(row.get("family") == link.get("family") and any(
                item.get("published_version") == link.get("published_version") and
                item.get("published_content_sha256") == link.get("published_content_sha256") and
                item.get("coder_loaded_version_id") == link.get("published_version") and
                item.get("coder_loaded_content_sha256") == link.get("published_content_sha256") and
                item.get("injection_verified") is True and
                item.get("model_call_observed") is True
                for item in row.get("loaded_task_skill_links", [])
            ) for link in changed_meta_links for row in arm_rows)
            gates[branch] = {"passed": meta_changed and bool(changed_meta_links) and loaded_changed,
                             "meta_changed": meta_changed,
                             "meta_changed_source_matched": bool(changed_meta_source_links),
                             "downstream_task_skill_published": bool(changed_meta_links),
                             "downstream_task_skill_loaded_with_verified_injection": loaded_changed,
                             "published_task_skills_based_on_changed_meta": changed_meta_links,
                             "changed_meta_task_skill_loaded_in_heldout": loaded_changed,
                             "task_skill_injection_evidence_by_family": injection_by_family,
                             "heldout_published_task_skill_usage": heldout_skill_usage,
                             "mechanism_proposal_and_evaluation_activity": activity,
                             "candidate_decisions": activity["candidate_decisions"],
                             "task_skill_runs": len(task_runs), "meta_skill_runs": len(meta_runs)}
    same_initial = len({json.dumps(snapshot, sort_keys=True) for snapshot in initial_snapshots.values()}) == 1
    mechanism_passed = all(gate.get("passed") is True for gate in gates.values()) and same_initial
    phase_tasks_passed = all(
        row.get("task", {}).get("status") == "completed" and
        row.get("oracle", {}).get("passed") is True and
        row.get("oracle", {}).get("acceptance_protected") is True
        for branch in BRANCHES for phase in ("phase_1", "phase_2")
        for row in phases.get(branch, {}).get(phase, {}).get("task_outcomes", [])
    )
    return gates, mechanism_passed, phase_tasks_passed


async def resume(parent_path: Path, output_path: Path, total_budget: int,
                fixture_dir: Path | None = None) -> dict:
    parent_path, output_path = parent_path.resolve(), output_path.resolve()
    if output_path.exists():
        raise ValueError(f"Refusing to overwrite resume report: {output_path}")
    if not parent_path.is_file():
        raise FileNotFoundError(parent_path)
    parent = json.loads(parent_path.read_text(encoding="utf-8"))
    if parent.get("status") != "budget_exhausted":
        raise ValueError("Parent report must be a budget_exhausted live run")
    if parent.get("execution_mode") != "live_http_docker":
        raise ValueError("Parent report is not a real HTTP/Docker evaluation")
    run_root = Path(parent["retained_run_root"]).resolve()
    ledger_path = run_root / "model-token-budget.sqlite3"
    if not ledger_path.is_file():
        raise FileNotFoundError(f"Retained token ledger is missing: {ledger_path}")
    prior_budget = parent.get("token_budget", {})
    old_limit = prior_budget.get("max_total_tokens")
    if not isinstance(old_limit, int) or total_budget <= old_limit:
        raise ValueError("New cumulative budget must be higher than the parent report's budget")

    recorded_fixture = parent.get("fixture_fingerprint")
    if not isinstance(recorded_fixture, dict) or not recorded_fixture.get("digest"):
        raise ValueError("Parent report lacks a fixture fingerprint; refusing continuation with unverifiable fixtures")
    selected_fixture_dir = fixture_dir or Path(recorded_fixture.get("path", evaluation.FIXTURES))
    selected_fixture_dir = selected_fixture_dir.expanduser().resolve()
    manifest, _, held = evaluation.load_dataset(selected_fixture_dir)
    manifest = evaluation.select_family(manifest, parent.get("family_filter"))
    if manifest.get("dataset_id") != parent.get("dataset_id"):
        raise ValueError("Dataset version differs from the parent report")
    current_fixture = evaluation.fixture_fingerprint(
        selected_fixture_dir, dataset_id=manifest.get("dataset_id"))
    if current_fixture.get("digest") != recorded_fixture.get("digest"):
        raise ValueError("Fixture contents differ from the parent report; refusing a mixed-dataset continuation")
    rows = parent.get("heldout_diagnostic", {}).get("paired_outcomes", [])
    existing_thresholds = (parent.get("thresholds", {}).get("task_skill_only", {})
                           or parent.get("threshold_configuration", {}))
    report.setdefault("phase_2_learning_trigger_diagnostic", evaluation.phase_2_trigger_diagnostic(
        manifest,
        batch_task_count=existing_thresholds.get(
            "batch_task_count", evaluation.PREVALIDATION_BATCH_TASK_COUNT),
        similar_issue_threshold=existing_thresholds.get(
            "similar_issue_threshold", evaluation.PREVALIDATION_SIMILAR_ISSUE_THRESHOLD)))
    expected_task_ids = [task_id for splits in manifest["families"].values()
                         for task_id in splits["held_out"]]
    expected_keys = {(branch, task_id) for branch in BRANCHES for task_id in expected_task_ids}
    row_keys = {(row.get("branch"), row.get("task_id")) for row in rows}
    if row_keys != expected_keys or len(rows) != len(expected_keys):
        raise ValueError("Parent report does not contain the complete planned arm/task matrix")

    snapshots = {branch: run_root / "snapshots" / branch for branch in BRANCHES}
    missing = [str(path / "evocode.db") for path in snapshots.values() if not (path / "evocode.db").is_file()]
    if missing:
        raise FileNotFoundError("Missing branch state snapshots: " + ", ".join(missing))
    snapshot_checks = {branch: verify_snapshot(path, parent, branch)
                       for branch, path in snapshots.items()}
    current_config = blackbox_runtime.load_live_config()
    recorded = parent.get("baseline_models_and_budgets", {})
    current_config_signature = {
        "local_model_id": current_config.models.local.model_id,
        "evolution_model_id": current_config.models.evolution.model_id,
        "local_model_budget": {"max_context_tokens": current_config.models.local.max_context_tokens,
                               "max_output_tokens": current_config.models.local.max_output_tokens},
        "evolution_model_budget": {"max_context_tokens": current_config.models.evolution.max_context_tokens,
                                   "max_output_tokens": current_config.models.evolution.max_output_tokens},
        "runtime_max_turns": current_config.runtime.max_turns,
        "evolution_timeout_seconds": current_config.evolution.timeout,
    }
    if current_config_signature != recorded:
        raise ValueError("Live model IDs/context/output/turn budgets differ from parent run; refusing a mixed-config comparison")
    recorded_config_fingerprint = parent.get("configuration_fingerprint")
    if not recorded_config_fingerprint:
        raise ValueError("Parent report lacks a configuration fingerprint; refusing unverifiable continuation")
    if recorded_config_fingerprint != evaluation.configuration_fingerprint(recorded):
        raise ValueError("Parent configuration fingerprint does not match its recorded configuration")
    if recorded_config_fingerprint != evaluation.configuration_fingerprint(current_config_signature):
        raise ValueError("Current configuration fingerprint differs from the parent report")

    ledger = TokenBudgetLedger(ledger_path, old_limit)
    before = ledger.snapshot()
    if before["pending_reserved_tokens"] or before["unknown_reserved_tokens"]:
        raise ValueError("Cannot resume while the retained ledger has pending/unknown token reservations")
    if before["reported_tokens"] != prior_budget.get("reported_tokens"):
        raise ValueError("Retained ledger usage differs from the parent report")

    backup_path = run_root / f"model-token-budget.before-resume-{uuid.uuid4().hex}.sqlite3"
    backup_sqlite(ledger_path, backup_path)
    snapshot_hashes = {branch: tree_sha256(path) for branch, path in snapshots.items()}

    report = json.loads(json.dumps(parent))
    checkpoint = {
        "mode": "heldout_only_continuation_from_retained_snapshots",
        "resume_id": uuid.uuid4().hex,
        "parent_report": str(parent_path),
        "parent_status": parent.get("status"),
        "parent_error": parent.get("error"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "old_cumulative_token_limit": old_limit,
        "new_cumulative_token_limit": total_budget,
        "token_ledger_backup": str(backup_path),
        "snapshot_sha256": snapshot_hashes,
        "snapshot_logical_state_checks": snapshot_checks,
        "fixture_fingerprint": current_fixture,
        "code_fingerprint": evaluation.code_fingerprint(),
        "configuration_fingerprint": recorded_config_fingerprint,
        "existing_observations_preserved": [],
        "attempted_keys": [],
        "additional_token_usage": 0,
    }
    for row in rows:
        if row.get("agent_call_started"):
            checkpoint["existing_observations_preserved"].append({"branch": row["branch"],
                "task_id": row["task_id"], "oracle_class": row.get("oracle_class")})
    report["resume_history"] = [*report.get("resume_history", []), checkpoint]
    report["status"] = "resume_prepared"
    report["max_total_model_tokens"] = total_budget
    report["large_benchmark_ready"] = False
    atomic_report(output_path, report)
    ledger.raise_limit(total_budget)
    report["token_budget"] = ledger.snapshot()
    report["status"] = "resuming_heldout"
    atomic_report(output_path, report)

    base_config = current_config
    max_turns = parent.get("max_turns_per_task", 40)
    if max_turns != 40:
        # The parent run caps the runtime per task; retain that exact cap.
        max_turns = max(1, min(40, int(max_turns)))

    try:
        resume_rows = sorted((row for row in rows if not row.get("agent_call_started")),
                             key=lambda item: (item.get("task_id", ""),
                                               list(BRANCHES).index(item.get("branch"))))
        for row in resume_rows:
            branch, task_id, family = row["branch"], row["task_id"], row["family"]
            task = held[task_id]
            task_root = run_root / "heldout-resume" / checkpoint["resume_id"] / branch / task_id
            workspace = task_root / "workspace"
            state_path = task_root / "state"
            if task_root.exists():
                raise FileExistsError(f"Resume destination already exists; refusing overwrite: {task_root}")
            evaluation.make_repo(workspace, task_id, task["source"], task_id)
            blackbox_runtime.clone_state(snapshots[branch], state_path)
            row["runner_failure"] = None
            row.setdefault("resume_attempt_history", []).append({
                "resume_id": checkpoint["resume_id"], "started_at": datetime.now(timezone.utc).isoformat(),
                "agent_call_started": False,
            })
            row["continuation_workspace"] = str(workspace)
            row["workspace_commit"] = subprocess.check_output(
                ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True).strip()
            config = base_config.model_copy(deep=True)
            config.data_dir = str(state_path)
            config.evolution.enabled = False
            config.evolution.auto_trigger_enabled = False
            config.evolution.auto_evaluate = False
            config.evolution.auto_publish = False
            config.evolution.freeze_meta_skill = BRANCHES[branch]["freeze_meta_skill"]
            config.evolution.selection_task_specs = []
            config.skills.enabled = True
            config.raw_rag.enabled = False
            config.raw_rag.index_when_disabled = False
            config.raw_rag.background_indexing = False
            config.runtime.execution_backend = "docker"
            config.runtime.max_turns = min(config.runtime.max_turns, max_turns)
            try:
                async with blackbox_runtime.Instance(
                    config, token_budget_db=ledger_path, max_total_tokens=total_budget
                ) as instance:
                    row["agent_call_started"] = True
                    result = await instance.run_task(workspace, task["prompt"], evaluation.repository_id(family))
                    row.update({"task": evaluation.compact(result), "trace": result.get("trace", {}),
                                "trace_artifacts": result.get("trace_artifacts", [])})
                    if result["status"] == "completed":
                        oracle = await blackbox_runtime.protected_acceptance(
                            config, workspace, task_root / "oracle", task["acceptance_file"],
                            task["acceptance"], task["acceptance_command"])
                        row["oracle"] = evaluation.acceptance_summary(oracle)
                        if oracle.get("passed") is False and oracle.get("acceptance_protected") is True:
                            row["oracle_failure_workspace"] = evaluation.workspace_diagnostic(workspace)
                    else:
                        row["oracle"] = None
                    lifecycle = report.get("lifecycle", {}).get(branch, {})
                    phase1_links = lifecycle.get("phase_1_boundary", {}).get("task_skill_links", [])
                    phase2_links = lifecycle.get("phase_2_task_skill_links", [])
                    loaded_by_phase = evaluation.loaded_task_skill_links_by_phase(
                        row.get("task", {}).get("coder_skill_injection_evidence"),
                        phase1_links, phase2_links, family=family)
                    row["loaded_phase_1_task_skill_links"] = loaded_by_phase["phase_1"]
                    row["loaded_phase_2_task_skill_links"] = loaded_by_phase["phase_2"]
                    row["loaded_task_skill_links"] = [
                        *loaded_by_phase["phase_1"], *loaded_by_phase["phase_2"]]
                    row["used_published_task_skill"] = bool(row["loaded_task_skill_links"])
                    row["used_published_phase_2_task_skill"] = bool(loaded_by_phase["phase_2"])
            except blackbox_runtime.TaskExecutionTimedOut as exc:
                row["agent_timeout"] = {"type": type(exc).__name__, "message": evaluation.safe_report_text(exc)}
                if getattr(exc, "observations", None):
                    row["trace"] = exc.observations.get("trace", {})
                    partial = evaluation.compact({"telemetry": exc.observations.get("telemetry", {})})
                    row["partial_agent_observation"] = {key: partial[key] for key in
                        ("coder_skill_hashes", "coder_skill_versions", "coder_skill_contexts",
                         "coder_skill_injection_evidence")}
                    row["trace_artifacts"] = exc.observations.get("trace_artifacts", [])
                row.update({"task": {"status": "timed_out", "failure": evaluation.safe_report_text(exc)},
                            "oracle": None})
            except Exception as exc:
                row["runner_failure"] = {"type": type(exc).__name__,
                                          "message": evaluation.safe_report_text(exc)}
                row.setdefault("task", {"status": "runner_error", "failure": evaluation.safe_report_text(exc)})
                row.setdefault("oracle", None)
            actual_hash = ((row.get("oracle") or {}).get("acceptance_file_sha256") or {}).get(task["acceptance_file"])
            oracle_verified = ((row.get("oracle") or {}).get("acceptance_protected") is True and
                               actual_hash == row.get("protected_acceptance_sha256"))
            row["oracle_class"] = ("runner_failure" if row.get("runner_failure") else
                                   "agent_timeout" if row.get("agent_timeout") else
                                   "agent_noncompletion" if row.get("task", {}).get("status") != "completed" else
                                   "unverified" if not oracle_verified else
                                   "pass" if row.get("oracle", {}).get("passed") is True else "assertion_fail")
            row["solved"] = row["oracle_class"] == "pass"
            row["resume_attempt_history"][-1].update({
                "agent_call_started": bool(row.get("agent_call_started")),
                "oracle_class": row.get("oracle_class"),
                "runner_failure": row.get("runner_failure"),
            })
            checkpoint["attempted_keys"].append({"branch": branch, "task_id": task_id,
                                                   "oracle_class": row["oracle_class"]})
            budget = ledger.snapshot()
            checkpoint["additional_token_usage"] = budget["reported_tokens"] - before["reported_tokens"]
            report["token_budget"] = budget
            report["resume_history"][-1] = checkpoint
            atomic_report(output_path, report)
            if budget["stop_reason"]:
                raise TokenBudgetExceeded(budget)
    except Exception as exc:
        budget = ledger.snapshot()
        report["status"] = "budget_exhausted" if budget.get("stop_reason") else "heldout_resume_failed"
        report["error_type"] = type(exc).__name__
        report["error"] = evaluation.safe_report_text(exc)
        report["token_budget"] = budget
    else:
        report["token_budget"] = ledger.snapshot()

    pass_rates, comparisons, complete, runner_failures = summarize_pairs(
        rows, list(manifest["families"]), expected_task_ids=expected_task_ids)
    mechanism_gates, mechanisms_passed, phase_tasks_passed = compute_mechanism_gates(report, rows)
    protected = all(row.get("oracle_class") in {"pass", "assertion_fail"}
                    for row in rows if row.get("task", {}).get("status") == "completed")
    report["heldout_diagnostic"] = {
        "status": "completed" if complete else "partial",
        "planned_outcomes": len(rows), "accounted_outcomes": len(rows),
        "pass_rates": pass_rates, "pairwise_comparisons": comparisons,
        "runner_failures": runner_failures, "paired_outcomes": rows,
    }
    report["gates"] = {
        "heldout_matrix_complete": complete,
        "heldout_runner_failures": runner_failures,
        "heldout_oracles_protected": protected,
        "all_phase_tasks_completed_with_protected_acceptance": phase_tasks_passed,
        "raw_rag_disabled": True,
        "autonomous_publication_enabled": True,
        "manual_evolution_actions_used": False,
        "mechanism_gates": mechanism_gates,
        "prevalidation_mechanisms_passed": mechanisms_passed,
        "large_benchmark_ready": (complete and runner_failures == 0 and protected and
                                   phase_tasks_passed and mechanisms_passed and
                                   report["token_budget"]["usage_unknown_requests"] == 0),
    }
    report["large_benchmark_ready"] = report["gates"]["large_benchmark_ready"]
    if report.get("status") == "resuming_heldout":
        report["status"] = "completed" if complete else "heldout_resume_incomplete"
    if report.get("status") == "completed":
        # Do not leave the parent's budget-stop error on a successfully resumed report.
        report.pop("error", None)
        report.pop("error_type", None)
    report["resume_history"][-1]["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["resume_history"][-1]["additional_token_usage"] = (
        report["token_budget"]["reported_tokens"] - before["reported_tokens"])
    atomic_report(output_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-total-tokens", type=int, required=True,
                        help="New cumulative cap shared with the retained parent ledger")
    parser.add_argument("--fixture-dir", type=Path,
                        help="Override the parent report's fixture directory (contents must match its digest)")
    args = parser.parse_args()
    if args.max_total_tokens <= 0:
        parser.error("--max-total-tokens must be positive")
    result = asyncio.run(resume(args.parent_report, args.output, args.max_total_tokens,
                                fixture_dir=args.fixture_dir))
    print(json.dumps({"status": result.get("status"), "error": result.get("error"),
                      "token_budget": result.get("token_budget"),
                      "heldout_diagnostic": {key: value for key, value in
                          result.get("heldout_diagnostic", {}).items() if key != "paired_outcomes"},
                      "gates": result.get("gates"),
                      "large_benchmark_ready": result.get("large_benchmark_ready")},
                     ensure_ascii=False, indent=2))
    if result.get("status") != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
