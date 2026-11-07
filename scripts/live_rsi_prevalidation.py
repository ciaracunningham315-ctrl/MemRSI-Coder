"""Offline preflight and opt-in live Meta-Skill comparison for EvoCode RSI fixtures.

Default preflight uses deterministic fixture source/reference pairs only. It makes
no model calls and does not claim to be an agent-run outcome. The live phase
uses real Planner-Coder material tasks and the existing EvolutionService paired
evaluator over the four disjoint held-out repositories.

    python scripts/live_rsi_prevalidation.py
    python scripts/live_rsi_prevalidation.py --phase live
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import difflib
import hashlib
from itertools import combinations
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/prevalidation/rsi"
PREVALIDATION_BATCH_TASK_COUNT = 2
PREVALIDATION_SIMILAR_ISSUE_THRESHOLD = 99
MODULES = {
    "api_compatibility_01": "lookup.py",
    "api_compatibility_02": "response.py",
    "input_validation_regression_01": "quantity.py",
    "input_validation_regression_02": "paging.py",
}


def module_name(task_id):
    if task_id in MODULES:
        return MODULES[task_id]
    return "api_material.py" if task_id.startswith("api_material_") else "validation_material.py"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_dataset(fixture_dir: Path | None = None):
    fixtures = (fixture_dir or FIXTURES).expanduser().resolve()
    manifest = json.loads((fixtures / "dataset.json").read_text(encoding="utf-8"))
    records = {}
    for split in ("learning", "phase_2", "selection"):
        for path in sorted((fixtures / "materials" / split).glob("*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            expected = {
                "learning": "synthetic_learning_material",
                "phase_2": "synthetic_phase_2_material",
                "selection": "synthetic_selection_material",
            }[split]
            if item.get("source_kind") != expected:
                raise ValueError(f"Incorrect provenance label: {path}")
            records[item["material_id"]] = item
    held = {}
    for path in sorted((fixtures / "held_out").glob("*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        if item["task_id"] in held:
            raise ValueError(f"Duplicate task ID: {item['task_id']}")
        held[item["task_id"]] = item
    positions, held_positions = [], []
    for family, splits in manifest["families"].items():
        if any(not splits[split] for split in ("learning", "phase_2", "selection", "held_out")):
            raise ValueError(f"Invalid split cardinality for {family}")
        for split in ("learning", "phase_2", "selection"):
            for material_id in splits[split]:
                if material_id not in records or records[material_id].get("family") != family:
                    raise ValueError(f"Missing/misclassified {split} material: {material_id}")
                positions.append(material_id)
        for task_id in splits["held_out"]:
            if task_id not in held or held[task_id].get("family") != family:
                raise ValueError(f"Missing/misclassified held-out task: {task_id}")
            held_positions.append(task_id)
    if len(positions) != len(set(positions)) or set(positions) & set(held_positions):
        raise ValueError("Learning, selection, and held-out sets must be distinct")
    if set(held) != set(held_positions):
        raise ValueError("Every held-out task must be assigned exactly once")
    return manifest, records, held


def fixture_fingerprint(fixture_dir: Path | None = None, *, dataset_id: str | None = None) -> dict:
    """Hash every fixture file, including files not currently referenced by the manifest."""
    fixtures = (fixture_dir or FIXTURES).expanduser().resolve()
    if not fixtures.is_dir():
        raise FileNotFoundError(fixtures)
    files = []
    for path in sorted(item for item in fixtures.rglob("*") if item.is_file()):
        relative = path.relative_to(fixtures).as_posix()
        files.append({"path": relative, "sha256": sha256(path.read_bytes())})
    canonical = "".join(f"{item['path']}\0{item['sha256']}\n" for item in files).encode("utf-8")
    return {"path": str(fixtures), "dataset_id": dataset_id,
            "files": files, "digest": sha256(canonical)}


def code_fingerprint() -> dict:
    """Fingerprint current Python sources and their delta from HEAD without storing source text."""
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    listing = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
        text=True).splitlines()
    source_paths = sorted({name for name in listing
                           if name.endswith(".py") and
                           (name.startswith("scripts/") or name.startswith("src/") or
                            name.startswith("tests/"))})
    files, changes = [], []
    for relative in source_paths:
        current_path = ROOT / relative
        current_hash = sha256(current_path.read_bytes()) if current_path.is_file() else None
        prior = subprocess.run(["git", "-C", str(ROOT), "show", f"HEAD:{relative}"],
                               capture_output=True)
        head_hash = sha256(prior.stdout) if prior.returncode == 0 else None
        files.append({"path": relative, "sha256": current_hash})
        if current_hash != head_hash:
            changes.append({"path": relative, "head_sha256": head_hash,
                            "current_sha256": current_hash})
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    delta = json.dumps(changes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"git_head": head, "python_sources": files,
            "source_digest": sha256(canonical),
            "uncommitted_diff_sha256": sha256(delta)}


def configuration_fingerprint(configuration: dict) -> str:
    """Hash the non-secret live model and budget settings recorded in the report."""
    encoded = json.dumps(configuration, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return sha256(encoded)


def acceptance(task, source):
    with tempfile.TemporaryDirectory(prefix="evocode-rsi-oracle-") as temp:
        root = Path(temp)
        module = root / module_name(task["task_id"])
        module.write_text(source, encoding="utf-8")
        oracle = root / "private_acceptance.py"
        oracle.write_text(task["acceptance"].replace("/workspace", str(root)), encoding="utf-8")
        result = subprocess.run([sys.executable, "-I", "-B", str(oracle)], cwd=root,
                                capture_output=True, text=True, timeout=10)
        return {"passed": result.returncode == 0, "returncode": result.returncode,
                "failure": (result.stderr or result.stdout).strip() or None}


def preflight(family=None, fixture_dir: Path | None = None):
    fixtures = (fixture_dir or FIXTURES).expanduser().resolve()
    manifest, materials, held = load_dataset(fixtures)
    fixture_info = fixture_fingerprint(fixtures, dataset_id=manifest.get("dataset_id"))
    manifest = select_family(manifest, family)
    rows = []
    for splits in manifest["families"].values():
        for task_id in splits["held_out"]:
            task = held[task_id]
            baseline = acceptance(task, task["source"])
            reference = acceptance(task, task["reference"])
            rows.append({"task_id": task_id, "family": task["family"],
                         "baseline": baseline, "reference": reference,
                         "protected_acceptance_sha256": sha256(task["acceptance"].encode()),
                         "passed": baseline["passed"] is False and reference["passed"] is True})
    phase_2_rows = []
    for family_key, splits in manifest["families"].items():
        for material_id in splits["phase_2"]:
            task = materials[material_id]["live_task"]
            baseline = acceptance(task, task["source"])
            reference = acceptance(task, task["reference"])
            phase_2_rows.append({"material_id": material_id, "task_id": task["task_id"],
                                 "family": family_key, "baseline": baseline,
                                 "reference": reference,
                                 "passed": baseline["passed"] is False and reference["passed"] is True})
    phase_2_learning_trigger_diagnostic = phase_2_trigger_diagnostic(
        manifest, batch_task_count=PREVALIDATION_BATCH_TASK_COUNT,
        similar_issue_threshold=PREVALIDATION_SIMILAR_ISSUE_THRESHOLD)
    all_checks = rows + phase_2_rows
    return {"status": "preflight_passed" if all_checks and all(r["passed"] for r in all_checks) else "preflight_failed",
            "execution_mode": "deterministic_fixture_preflight_only", "model_calls": 0,
            "dataset_id": manifest["dataset_id"],
            "fixture_fingerprint": fixture_info,
            "code_fingerprint": code_fingerprint(),
            "family_filter": family, "families": list(manifest["families"]),
            "split_counts": {"families": len(manifest["families"]),
                             "learning": sum(len(s["learning"]) for s in manifest["families"].values()),
                             "phase_2": sum(len(s["phase_2"]) for s in manifest["families"].values()),
                             "selection": sum(len(s["selection"]) for s in manifest["families"].values()),
                             "held_out": len(rows)}, "rows": rows,
            "phase_2_oracle_checks": phase_2_rows,
            "phase_2_learning_trigger_diagnostic": phase_2_learning_trigger_diagnostic}


def select_family(manifest, family=None):
    if family is None:
        return manifest
    if family not in manifest["families"]:
        raise ValueError(f"Unknown family {family!r}; choose from {', '.join(sorted(manifest['families']))}")
    return {**manifest, "families": {family: manifest["families"][family]}}


def phase_2_trigger_diagnostic(manifest, *, batch_task_count, similar_issue_threshold):
    """Describe by-count reachability of automatic triggers; never reject a fixture."""
    families = {}
    for family, splits in manifest.get("families", {}).items():
        task_count = len(splits.get("phase_2", []))
        batch_ready = task_count >= max(1, batch_task_count)
        families[family] = {
            "task_count": task_count,
            "batch_task_count": batch_task_count,
            "similar_issue_threshold": similar_issue_threshold,
            "enough_tasks_for_repository_batch": batch_ready,
            "enough_tasks_for_repeated_issue": task_count >= max(1, similar_issue_threshold),
            "message": ("phase_2 count can reach repository_batch trigger"
                        if batch_ready else
                        "phase_2 count is below repository_batch threshold; repeated_issue depends on matching_issue_tasks"),
        }
    return {"source": "EvolutionService.on_task_finished repository_batch and repeated_issue checks",
            "diagnostic_only": True,
            "batch_task_count": batch_task_count,
            "similar_issue_threshold": similar_issue_threshold,
            "families": families}


def make_repo(root, task_id, source, commit_message):
    root.mkdir(parents=True)
    (root / module_name(task_id)).write_text(source, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=EvoCode prevalidation",
                    "-c", "user.email=fixture@example.invalid", "commit", "-qm", commit_message], check=True)
    return sha256(source.encode("utf-8"))


def compact(result):
    telemetry = result.get("telemetry") or {}
    return {"task_id": result.get("task_id"),
            "status": result.get("status"), "passed": result.get("passed"),
            "acceptance_protected": result.get("acceptance_protected"),
            "tool_calls": result.get("tool_calls", telemetry.get("tool_calls")),
            "model_calls": telemetry.get("model_calls"),
            "tokens": result.get("tokens", telemetry.get("reported_tokens", telemetry.get("total_tokens"))),
            "tokens_complete": telemetry.get("tokens_complete"),
            "duration_seconds": result.get("duration_seconds", telemetry.get("duration_seconds")),
            "acceptance_file_sha256": result.get("acceptance_file_sha256", {}),
            "coder_skill_hashes": skill_hashes(result),
            "coder_skill_versions": sorted({
                str(version.get("version_id"))
                for context in telemetry.get("skill_contexts", [])
                if isinstance(context, dict) and context.get("role") == "coder"
                for version in context.get("skill_versions", []) if isinstance(version, dict)
                if version.get("version_id")
            }),
            "coder_skill_contexts": [{key: item.get(key) for key in
                                      ("context_id", "event_id", "event_index", "selection_point",
                                       "skill_refs", "model_call_observed", "injection_verified",
                                       "skill_versions")}
                                     for item in telemetry.get("skill_contexts", [])],
            "coder_skill_injection_evidence": [
                {"context_id": context.get("context_id"),
                 "injection_verified": context.get("injection_verified") is True,
                 "model_call_observed": context.get("model_call_observed") is True,
                 "skill_versions": [{"version_id": version.get("version_id"),
                                     "content_sha256": version.get("content_sha256")}
                                    for version in context.get("skill_versions", [])
                                    if isinstance(version, dict) and version.get("version_id") and
                                    version.get("content_sha256")]}
                for context in telemetry.get("skill_contexts", [])
                if isinstance(context, dict) and context.get("role") == "coder" and
                context.get("injection_verified") is True and
                context.get("model_call_observed") is True and context.get("skill_versions")
            ],
            "skill_selections": [{key: event.get(key) for key in
                                  ("event_id", "seq", "event_index", "phase", "loaded_skill_ids",
                                   "candidates", "limit") if key in event}
                                 for event in (result.get("trace") or {}).get("key_events", [])
                                 if event.get("event_type") == "skill.selection"],
            "trace_artifacts": result.get("trace_artifacts", []),
            "failure": safe_report_text(result.get("error") or result.get("failure_reason"))
            if result.get("error") or result.get("failure_reason") else None}


def acceptance_summary(oracle):
    output = str(oracle.get("acceptance_output") or "")
    reason = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), None)
    if reason:
        reason = safe_report_text(reason)
    return {key: oracle.get(key) for key in
            ("passed", "acceptance_protected", "acceptance_file_sha256") if key in oracle} | {
                "acceptance_reason": reason[:400] if reason else None}


def safe_report_text(value):
    value = re.sub(r"(?i)\b(sk-[A-Za-z0-9_-]{12,})", "[REDACTED_TOKEN]", str(value))
    value = re.sub(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]{12,}", r"\1[REDACTED_TOKEN]", value)
    value = re.sub(r"(?i)\b(api[_-]?key\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", value)
    return value[:1000]


def workspace_diagnostic(workspace: Path, *, max_diff_chars=16000):
    """Capture bounded Python-only state evidence without copying whole files."""
    workspace = workspace.resolve()
    status = subprocess.run(["git", "-C", str(workspace), "status", "--short", "--untracked-files=all"],
                            capture_output=True, text=True, check=True).stdout.splitlines()
    paths = subprocess.run(["git", "-C", str(workspace), "ls-files", "--cached", "--others",
                            "--exclude-standard"], capture_output=True, text=True, check=True).stdout.splitlines()
    files, chunks = [], []
    for relative in paths:
        path = Path(relative)
        if path.suffix != ".py" or path.is_absolute() or ".." in path.parts:
            continue
        current_path = workspace / path
        if not current_path.is_file():
            continue
        current = current_path.read_text(encoding="utf-8", errors="replace")
        prior = subprocess.run(["git", "-C", str(workspace), "show", f"HEAD:{relative}"],
                               capture_output=True, text=True)
        before = prior.stdout if prior.returncode == 0 else ""
        files.append({"path": relative, "sha256": sha256(current.encode("utf-8")),
                      "bytes": current_path.stat().st_size, "tracked_at_start": prior.returncode == 0})
        if current != before:
            chunks.extend(difflib.unified_diff(before.splitlines(keepends=True),
                                               current.splitlines(keepends=True),
                                               fromfile=f"a/{relative}", tofile=f"b/{relative}"))
    diff = "".join(chunks)
    return {"git_status": status, "python_files": files,
            "python_diff": diff[:max_diff_chars],
            "python_diff_truncated": len(diff) > max_diff_chars,
            "diff_limit_chars": max_diff_chars}


def persist_report(path: Path | None, report):
    if path is None:
        return
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def skill_hashes(result):
    """Extract hashes explicitly recorded for skills injected into the Coder."""
    found = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"coder_skill_hashes", "skill_injection_hashes"}:
                    if isinstance(item, (list, tuple, set)):
                        found.update(str(x) for x in item if x)
                    elif item:
                        found.add(str(item))
                visit(item)
            contexts = value.get("skill_contexts")
            if isinstance(contexts, list):
                for context in contexts:
                    if not isinstance(context, dict) or context.get("role") != "coder":
                        continue
                    for version in context.get("skill_versions", []) if isinstance(context, dict) else []:
                        digest = version.get("content_sha256") if isinstance(version, dict) else None
                        if digest:
                            found.add(str(digest))
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(result.get("telemetry") or {})
    visit(result.get("events") or [])
    return sorted(found)


def token_cost(result):
    telemetry = result.get("telemetry") or {}
    value = result.get("tokens", telemetry.get("reported_tokens", telemetry.get("total_tokens", 0)))
    return int(value or 0)


def token_value(result):
    """Return a token count only when telemetry actually reports one."""
    telemetry = result.get("telemetry") or {}
    value = result.get("tokens", telemetry.get("reported_tokens", telemetry.get("total_tokens")))
    return int(value) if isinstance(value, (int, float)) else None


def rows_from_response(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "runs", "versions", "data"):
            if isinstance(value.get(key), list):
                return value[key]
    return []


def task_spec(workspace, task, family, oracle_path):
    commit = subprocess.check_output(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True
    ).strip()
    return {"repo_path": str(workspace), "repository_id": repository_id(family),
            "prompt": task["prompt"], "acceptance_command": task["acceptance_command"],
            "acceptance_files": [task["acceptance_file"]], "commit": commit,
            "protected_acceptance_paths": {task["acceptance_file"]: str(oracle_path.resolve())},
            "family": family}


def repository_id(family):
    """Keep project-skill scope stable within a branch and fixture family."""
    return f"rsi-prevalidation-{family}"


def loaded_task_skill_links(injection_evidence, links, *, family=None):
    """Join exact, verified Coder injection evidence to the published phase run."""
    evidence = [item for item in injection_evidence or []
                if isinstance(item, dict) and item.get("injection_verified") is True and
                item.get("model_call_observed") is True]
    loaded_versions = {}
    for context in evidence:
        for version in context.get("skill_versions", []):
            if isinstance(version, dict) and version.get("version_id") and version.get("content_sha256"):
                loaded_versions.setdefault(version["version_id"], []).append(
                    {**version, "context_id": context.get("context_id"),
                     "injection_verified": True, "model_call_observed": True})
    matched = []
    for link in links or []:
        if (link.get("status") != "published" or not link.get("published_version") or
                (family is not None and link.get("family") != family)):
            continue
        exact = [item for item in loaded_versions.get(link["published_version"], [])
                 if item.get("content_sha256") == link.get("published_content_sha256")]
        if exact:
            matched.append({**link, "coder_loaded_version_id": link["published_version"],
                            "coder_loaded_content_sha256": link.get("published_content_sha256"),
                            "injection_verified": True, "model_call_observed": True,
                            "injection_evidence": exact})
    return matched


def loaded_task_skill_links_by_phase(injection_evidence, phase1_links, phase2_links, *, family=None):
    """Join verified held-out Coder loads to published TaskSkill links from each phase."""
    return {
        "phase_1": loaded_task_skill_links(injection_evidence, phase1_links, family=family),
        "phase_2": loaded_task_skill_links(injection_evidence, phase2_links, family=family),
    }


def lifecycle_tokens(runs):
    """Count model output once per proposal and paired task execution."""
    total, model_calls = 0, 0
    missing_usage_runs = []
    for run in runs:
        payload = run.get("payload") or {}
        usage = payload.get("usage")
        attempts = list(payload.get("proposal_attempts") or [])
        for stage in payload.get("meta_stages") or []:
            attempts.extend(stage.get("proposal_attempts") or [])
        if (any(attempt.get("usage_complete") is False for attempt in attempts) or
                run.get("status") in {"failed", "interrupted"} and not (usage or {}).get("total_tokens")):
            missing_usage_runs.append(run.get("run_id"))
        if isinstance(usage, dict):
            total += int(usage.get("total_tokens", 0) or 0)
        # Meta stages are mirrored inside payload.evaluation; count their
        # top-level proposal usage only once.
        for stage in payload.get("meta_stages", []):
            stage_usage = stage.get("usage") or {}
            total += int(stage_usage.get("total_tokens", 0) or 0)
        evaluation_rows = (payload.get("evaluation") or {}).get("rows", [])
        for row in evaluation_rows:
            for side in ("baseline", "candidate"):
                outcome = row.get(side) or {}
                if isinstance(outcome.get("tokens"), int):
                    total += outcome["tokens"]
                if isinstance(outcome.get("model_calls"), int):
                    model_calls += outcome["model_calls"]
    return {"reported_tokens": total, "paired_evaluator_model_calls": model_calls,
            "failed_proposal_usage_missing": missing_usage_runs,
            "reported_tokens_are_lower_bound": bool(missing_usage_runs)}


def proposal_reliability(runs):
    groups = []
    for run in runs:
        payload = run.get("payload") or {}
        for source in [payload, *(payload.get("meta_stages") or [])]:
            if source.get("proposal_attempts"):
                groups.append(source["proposal_attempts"])
    attempts = [attempt for group in groups for attempt in group]
    return {
        "observed_proposals": len(groups),
        "first_attempt_valid": sum(group[0].get("status") == "valid" for group in groups),
        "repaired_valid": sum(len(group) > 1 and group[-1].get("status") == "valid" for group in groups),
        "final_invalid": sum(group[-1].get("status") != "valid" for group in groups),
        "structured_valid": sum(group[-1].get("status") == "valid" and
                                group[-1].get("representation") == "structured_body" for group in groups),
        "attempts": len(attempts),
        "recorded_proposal_tokens": sum((attempt.get("usage") or {}).get("total_tokens", 0)
                                        for attempt in attempts),
        "attempts_with_unknown_usage": sum(attempt.get("usage_complete") is False for attempt in attempts),
    }


def summarize_attempts(attempts):
    return [{key: attempt.get(key) for key in
             ("attempt", "status", "representation", "error_type", "finish_reason", "usage",
              "usage_complete", "response_id", "raw_output_artifact_id")}
            for attempt in attempts or []]


def summarize_run(run):
    """Keep lifecycle reporting useful without exposing trace text or skill contents."""
    payload = run.get("payload") or {}
    candidate = payload.get("candidate") or {}
    base_meta = payload.get("base_meta") or {}
    evaluation = payload.get("evaluation") or {}
    stages = payload.get("meta_stages") or []
    summary = {
        "run_id": run.get("run_id"), "kind": run.get("kind"), "status": run.get("status"),
        "task_id": run.get("task_id"), "meta_skill_version_id": run.get("meta_skill_version_id"),
        "source_meta_content_sha256": sha256(base_meta["content"].encode("utf-8"))
            if isinstance(base_meta.get("content"), str) else None,
        "trigger": payload.get("trigger"),
        "reason": safe_report_text(payload.get("reason")) if payload.get("reason") else None,
        "error": safe_report_text(payload.get("error")) if payload.get("error") else None,
        "proposal_attempts": summarize_attempts(payload.get("proposal_attempts")),
        "candidate": {key: candidate.get(key) for key in
                      ("version_id", "name", "level", "content_sha256", "status") if key in candidate},
        "usage": {key: (payload.get("usage") or {}).get(key) for key in
                  ("total_tokens", "prompt_tokens", "completion_tokens")
                  if key in (payload.get("usage") or {})},
    }
    published = payload.get("published_version") or {}
    if published:
        summary["published_version"] = {key: published.get(key) for key in
                                         ("version_id", "skill_id", "content_sha256", "status")
                                         if key in published}
    if evaluation:
        summary["evaluation"] = {
            "status": evaluation.get("status"), "passed": evaluation.get("passed"),
            "reasons": [safe_report_text(item) for item in evaluation.get("reasons", [])]
                if isinstance(evaluation.get("reasons"), list) else evaluation.get("reasons"),
            "target_metric": evaluation.get("target_metric"),
            "baseline": evaluation.get("baseline"), "candidate": evaluation.get("candidate"),
            "rows": [{"baseline": compact(row.get("baseline") or {}),
                      "candidate": compact(row.get("candidate") or {})}
                     for row in evaluation.get("rows", [])],
        }
    if stages:
        summary["meta_stages"] = [{
            "branch": stage.get("branch"),
            "proposal_attempts": summarize_attempts(stage.get("proposal_attempts")),
            "candidate": {key: (stage.get("candidate") or {}).get(key) for key in
                          ("version_id", "name", "level", "content_sha256", "status")
                          if key in (stage.get("candidate") or {})},
            "usage": {key: (stage.get("usage") or {}).get(key) for key in
                      ("total_tokens", "prompt_tokens", "completion_tokens")
                      if key in (stage.get("usage") or {})},
        } for stage in stages]
    return summary


def task_skill_links(phase_data):
    outcomes = {(row.get("task") or {}).get("task_id") or row.get("task_id"): row
                for row in phase_data.get("task_outcomes", [])}
    links = []
    for run in phase_data.get("task_skill_runs", []):
        candidate = run.get("candidate") or {}
        published = run.get("published_version") or {}
        task_outcome = outcomes.get(run.get("task_id")) or {}
        active_meta = task_outcome.get("active_meta_before") or {}
        source_version_id = run.get("meta_skill_version_id")
        source_hash = run.get("source_meta_content_sha256")
        active_version_id = active_meta.get("version_id")
        if active_version_id:
            meta_matches = (source_version_id == active_version_id and
                            source_hash == active_meta.get("content_sha256"))
        elif active_meta.get("source") == "builtin":
            meta_matches = source_version_id is None and source_hash == active_meta.get("content_sha256")
        else:
            meta_matches = None
        links.append({"run_id": run.get("run_id"), "task_id": run.get("task_id"),
                      "family": task_outcome.get("family"),
                      "source_meta_skill_version_id": source_version_id,
                      "source_meta_content_sha256": source_hash,
                      "active_meta_at_source_task": active_meta,
                      "source_meta_matches_active_before": meta_matches,
                      "task_skill_version_id": candidate.get("version_id"),
                      "status": run.get("status"),
                      "published_version": published.get("version_id"),
                      "published_content_sha256": published.get("content_sha256")})
    return links


def lifecycle_activity(task_runs, meta_runs):
    """Report observed proposal/evaluation and candidate decisions without changing policy."""
    runs = [("task", run) for run in task_runs] + [("meta", run) for run in meta_runs]
    proposal_count = evaluation_count = 0
    evaluations = []
    candidate_decisions = []
    for kind, run in runs:
        attempts = run.get("proposal_attempts") or []
        stages = run.get("meta_stages") or []
        proposal_count += len(attempts) + sum(
            len(stage.get("proposal_attempts") or []) for stage in stages)
        evaluation = run.get("evaluation")
        if isinstance(evaluation, dict):
            evaluation_count += 1
            evaluations.append({"kind": kind, "run_id": run.get("run_id"),
                                "status": evaluation.get("status"),
                                "passed": evaluation.get("passed")})
        candidate = run.get("candidate") or {}
        published = run.get("published_version") or {}
        if candidate or published or run.get("status") in {"published", "needs_review"}:
            candidate_decisions.append({
                "kind": kind, "run_id": run.get("run_id"),
                "run_status": run.get("status"),
                "candidate_version_id": candidate.get("version_id"),
                "candidate_status": candidate.get("status"),
                "published_version_id": published.get("version_id"),
                "published_status": published.get("status"),
            })
    return {"proposal_count": proposal_count, "evaluation_count": evaluation_count,
            "evaluations": evaluations, "candidate_decisions": candidate_decisions,
            "published_count": sum(item.get("run_status") == "published" or
                                    item.get("published_version_id") is not None
                                    for item in candidate_decisions),
            "needs_review_count": sum(item.get("run_status") == "needs_review" or
                                       item.get("candidate_status") == "needs_review"
                                       for item in candidate_decisions)}


async def live(*, family: str | None = None, max_turns: int = 40,
               max_total_tokens: int = 1_500_000, retain_dir: Path | None = None,
               report_path: Path | None = None, fixture_dir: Path | None = None):
    """Run a budgeted three-arm Agent learning comparison over the real HTTP harness."""
    if type(max_total_tokens) is not int or max_total_tokens < 1:
        raise ValueError("max_total_tokens must be a positive integer")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts import blackbox_runtime
    task_timeout_error = getattr(blackbox_runtime, "TaskExecutionTimedOut", TimeoutError)

    fixtures = (fixture_dir or FIXTURES).expanduser().resolve()
    manifest, materials, held = load_dataset(fixtures)
    manifest = select_family(manifest, family)
    fixture_info = fixture_fingerprint(fixtures, dataset_id=manifest.get("dataset_id"))
    source_info = code_fingerprint()
    base_config = blackbox_runtime.load_live_config()
    baseline_models = {
        "local_model_id": base_config.models.local.model_id,
        "evolution_model_id": base_config.models.evolution.model_id,
        "local_model_budget": {"max_context_tokens": base_config.models.local.max_context_tokens,
                               "max_output_tokens": base_config.models.local.max_output_tokens},
        "evolution_model_budget": {"max_context_tokens": base_config.models.evolution.max_context_tokens,
                                   "max_output_tokens": base_config.models.evolution.max_output_tokens},
        "runtime_max_turns": base_config.runtime.max_turns,
        "evolution_timeout_seconds": base_config.evolution.timeout,
    }
    report = {"status": "running", "execution_mode": "live_http_docker",
              "dataset_id": manifest["dataset_id"],
              "fixture_fingerprint": fixture_info,
              "code_fingerprint": source_info,
              "family_filter": family,
              "families": list(manifest["families"]),
              "design": {"branches": ["no_learning", "task_skill_only", "full_rsi"],
                         "phase_1": "learning fixtures run as ordinary HTTP Agent tasks",
                         "phase_2": "separate phase_2 fixtures run after phase_1 idle boundary, under the actual active Meta version even when it is builtin or unchanged",
                         "selection": "server-side candidate evaluation only; no phase task execution",
                         "heldout": "frozen after phase_2 and cloned independently for each planned task and branch",
                         "phase_boundaries": "wait for HTTP Agent jobs to become idle after each phase_1 and phase_2 task and at phase boundaries so later tasks and families see prior publications",
                         "arm_meanings": {
                             "no_learning": "evolution disabled; uses the same initial skill state without learned updates",
                             "task_skill_only": "Task Skill evolution and publication enabled; Meta Skill frozen",
                             "full_rsi": "Task Skill and Meta Skill evolution/publication enabled",
                         },
                         "autonomous_publication": True, "manual_evolution_actions": False},
              "threshold_profile": "mechanism_prevalidation_not_production",
              "threshold_configuration": {"batch_task_count": 2, "similar_issue_threshold": 99,
                                           "meta_review_after_runs": 1},
              "threshold_effects_claimed": False,
              "baseline_models_and_budgets": baseline_models,
              "configuration_fingerprint": configuration_fingerprint(baseline_models),
              "decision_thresholds": {
                  "task_solved": "terminal status completed AND protected acceptance passed AND acceptance_protected is true",
                  "paired_win": "exactly one branch solves the same held-out task; two failures count as a tie",
                  "runner_failure": "setup, HTTP, worker, or oracle-runner exception; an Agent execution timeout counts as noncompletion",
                  "efficacy_claim": "none; this prevalidation is descriptive and does not establish statistical significance"
              },
              "max_turns_per_task": max_turns,
              "max_total_model_tokens": max_total_tokens,
              "token_budget_policy": "one cross-process SQLite ledger shared by every arm, phase, held-out task, and evaluation child; before send reserve serialized UTF-8 request bytes plus the full output limit and a safety margin, then settle to provider usage; if usage is unknown, keep the reservation"}
    persist_report(report_path, report)
    if retain_dir is not None:
        retain_dir = retain_dir.expanduser().resolve()
        retain_dir.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.mkdtemp(prefix="evocode-rsi-blackbox-", dir=retain_dir)
        temporary_context = nullcontext(temporary)
    else:
        temporary_context = tempfile.TemporaryDirectory(prefix="evocode-rsi-blackbox-")
    with temporary_context as temporary:
        root = Path(temporary)
        from evocode.token_budget import TokenBudgetLedger
        token_budget_db = root / "model-token-budget.sqlite3"
        token_budget = TokenBudgetLedger(token_budget_db, max_total_tokens)
        if retain_dir is not None:
            report["retained_run_root"] = str(root)
        persist_report(report_path, report)
        branches = {
            "no_learning": {"evolution_enabled": False, "freeze_meta_skill": True},
            "task_skill_only": {"evolution_enabled": True, "freeze_meta_skill": True},
            "full_rsi": {"evolution_enabled": True, "freeze_meta_skill": False},
        }
        trace_splits = {name: {"phase_1": [], "phase_2": [], "selection": [], "held_out": []}
                        for name in branches}
        phase_reports = {name: {phase: {"reported_tokens": 0, "token_values_unknown": 0,
                                        "attempted_tasks": 0, "task_count": 0,
                                        "runner_failures": [], "agent_timeouts": [],
                                        "task_outcomes": []}
                                for phase in ("phase_1", "phase_2")}
                         for name in branches}
        lifecycle = {name: {} for name in branches}
        heldout_rows = []
        heldout_by_key = {}
        for branch in branches:
            for family, splits in manifest["families"].items():
                for task_id in splits["held_out"]:
                    row = {"task_id": task_id, "family": family, "branch": branch,
                           "protected_acceptance_sha256": sha256(held[task_id]["acceptance"].encode()),
                           "repository_id": repository_id(family),
                           "runner_failure": {"type": "not_attempted",
                                              "message": "Runner did not reach this planned held-out task."},
                           "agent_call_started": False,
                           "task": {"status": "runner_error", "failure": "not attempted"},
                           "oracle": None, "oracle_class": "runner_failure", "solved": False}
                    heldout_by_key[(branch, task_id)] = row
                    heldout_rows.append(row)
        def checkpoint():
            report.update({"trace_splits": trace_splits, "phases": phase_reports,
                           "lifecycle": lifecycle,
                           "token_budget": token_budget.snapshot(),
                           "heldout_diagnostic": {"status": "partial",
                               "planned_outcomes": len(heldout_rows),
                               "accounted_outcomes": len(heldout_rows),
                               "paired_outcomes": heldout_rows}})
            persist_report(report_path, report)
        branch_snapshots = {}
        thresholds = {}
        try:
            learning_materials = [(family, materials[item])
                                  for family, splits in manifest["families"].items()
                                  for item in splits["learning"]]
            phase_2_materials = [(family, materials[item])
                                 for family, splits in manifest["families"].items()
                                 for item in splits["phase_2"]]
            selection_materials = [(family, materials[item])
                                   for family, splits in manifest["families"].items()
                                   for item in splits["selection"]]
            for branch in branches:
                try:
                    branch_root = root / branch
                    selection_specs = []
                    for family, material in selection_materials:
                        task = material["live_task"]
                        workspace = branch_root / "selection" / material["material_id"]
                        make_repo(workspace, task["task_id"], task["source"], material["material_id"])
                        oracle_path = root / "private-oracles" / f"{material['material_id']}.py"
                        oracle_path.parent.mkdir(parents=True, exist_ok=True)
                        oracle_path.write_text(task["acceptance"], encoding="utf-8")
                        selection_specs.append(task_spec(workspace, task, family, oracle_path))
                        trace_splits[branch]["selection"].append(task["task_id"])

                    config = copy.deepcopy(base_config)
                    config.data_dir = str(branch_root / "state")
                    config.evolution.enabled = branches[branch]["evolution_enabled"]
                    config.evolution.auto_trigger_enabled = branches[branch]["evolution_enabled"]
                    config.evolution.auto_evaluate = branches[branch]["evolution_enabled"]
                    config.evolution.auto_publish = branches[branch]["evolution_enabled"]
                    config.evolution.freeze_meta_skill = branches[branch]["freeze_meta_skill"]
                    config.evolution.selection_task_specs = selection_specs
                    config.evolution.allow_remote_code_context = True
                    # This profile exercises the complete learning path with the small
                    # fixture set. It is intentionally separate from production policy.
                    config.evolution.batch_task_count = 2
                    config.evolution.similar_issue_threshold = 99
                    config.evolution.meta_review_after_runs = 1
                    thresholds[branch] = {
                        "evolution_enabled": config.evolution.enabled,
                        "freeze_meta_skill": config.evolution.freeze_meta_skill,
                        "batch_task_count": config.evolution.batch_task_count,
                        "similar_issue_threshold": config.evolution.similar_issue_threshold,
                        "meta_review_after_runs": config.evolution.meta_review_after_runs,
                        "profile": report["threshold_profile"],
                    }
                    config.skills.enabled = True
                    config.raw_rag.enabled = False
                    config.raw_rag.index_when_disabled = False
                    config.raw_rag.background_indexing = False
                    config.runtime.execution_backend = "docker"
                    config.runtime.max_turns = min(config.runtime.max_turns, max_turns)
                    initial_skill_versions = None
                    initial_active_meta = None
                    async with blackbox_runtime.Instance(
                        config, token_budget_db=token_budget_db, max_total_tokens=max_total_tokens
                    ) as instance:
                        initial_skill_versions = rows_from_response(
                            await instance.request("GET", "/api/skills/versions"))
                        initial_active_meta = await instance.request("GET", "/api/evolution/active-meta")
                        lifecycle[branch]["initial_active_meta"] = initial_active_meta
                        lifecycle[branch]["initial_skill_versions"] = [
                            {key: item.get(key) for key in ("skill_id", "version_id", "level", "content_sha256", "status")
                             if key in item} for item in initial_skill_versions]
                        public_config = await instance.request("GET", "/api/config")
                        public_json = json.dumps(public_config, ensure_ascii=False)
                        if "selection_task_specs" in (public_config.get("evolution") or {}) or any(
                            path in public_json for spec in selection_specs
                            for path in spec["protected_acceptance_paths"].values()
                        ):
                            raise RuntimeError("Private selection oracle paths leaked through the config API")
                        observed_run_ids = {
                            run.get("run_id") for run in rows_from_response(
                                await instance.request("GET", "/api/evolution"))
                        }
                        for phase, phase_materials in (("phase_1", learning_materials),
                                                       ("phase_2", phase_2_materials)):
                            phase_data = phase_reports[branch][phase]
                            if phase == "phase_2":
                                # The phase-1 idle boundary already captured the actual
                                # Meta selection. Its state is an observation, never a gate.
                                lifecycle[branch]["phase_2_starts_under_meta"] = (
                                    lifecycle[branch]["phase_1_boundary"]["active_meta"])
                            for family, material in phase_materials:
                                task = material["live_task"]
                                workspace = branch_root / phase / material["material_id"]
                                outcome = {"task_id": task["task_id"], "material_id": material["material_id"],
                                           "family": family, "repository_id": repository_id(family),
                                           "agent_call_started": False,
                                           "runner_failure": None}
                                try:
                                    outcome["active_meta_before"] = await instance.request(
                                        "GET", "/api/evolution/active-meta")
                                except Exception as exc:
                                    outcome["active_meta_observation_failure"] = {
                                        "type": type(exc).__name__, "message": safe_report_text(exc)}
                                try:
                                    make_repo(workspace, task["task_id"], task["source"], material["material_id"])
                                    phase_data["attempted_tasks"] += 1
                                    outcome["agent_call_started"] = True
                                    result = await instance.run_task(workspace, task["prompt"], repository_id(family))
                                    events = result.get("events") or []
                                    oracle = await blackbox_runtime.protected_acceptance(
                                        config, workspace,
                                        branch_root / f"{phase}-oracles" / material["material_id"],
                                        task["acceptance_file"], task["acceptance"],
                                        task["acceptance_command"],
                                    )
                                    outcome.update({"task": compact(result), "oracle": oracle,
                                                    "trace": result.get("trace", {}),
                                                    "code_patch_observed": any(event.get("event_type") == "code.patch"
                                                                                for event in events),
                                                    "automatic_trigger_checks": [event.get("payload", {})
                                                        for event in events if event.get("event_type") == "evolution.trigger_checked"]})
                                    outcome["oracle"] = acceptance_summary(oracle)
                                    if oracle.get("passed") is False and oracle.get("acceptance_protected") is True:
                                        outcome["oracle_failure_workspace"] = workspace_diagnostic(workspace)
                                    if phase == "phase_2":
                                        prior_links = lifecycle[branch]["phase_1_boundary"].get("task_skill_links", [])
                                        outcome["loaded_phase_1_task_skill_links"] = loaded_task_skill_links(
                                            outcome["task"].get("coder_skill_injection_evidence"), prior_links,
                                            family=family)
                                    phase_data["task_count"] += 1
                                    token_count = token_value(result)
                                    if token_count is None:
                                        phase_data["token_values_unknown"] += 1
                                    else:
                                        phase_data["reported_tokens"] += token_count
                                    trace_splits[branch][phase].append(result.get("task_id"))
                                except task_timeout_error as exc:
                                    outcome["task"] = {"status": "timed_out", "failure": safe_report_text(exc)}
                                    if getattr(exc, "observations", None):
                                        outcome["trace"] = exc.observations.get("trace", {})
                                        partial = compact({"telemetry": exc.observations.get("telemetry", {})})
                                        outcome["partial_agent_observation"] = {
                                            "coder_skill_hashes": partial["coder_skill_hashes"],
                                            "coder_skill_versions": partial["coder_skill_versions"],
                                            "coder_skill_contexts": partial["coder_skill_contexts"],
                                            "coder_skill_injection_evidence": partial["coder_skill_injection_evidence"]}
                                        outcome["task"]["trace_artifacts"] = exc.observations.get("trace_artifacts", [])
                                    if getattr(exc, "observation_failure", None):
                                        outcome["trace_observation_failure"] = safe_report_text(exc.observation_failure)
                                    outcome["oracle"] = None
                                    phase_data["token_values_unknown"] += 1
                                    phase_data["agent_timeouts"].append({"task_id": task["task_id"],
                                        "type": type(exc).__name__, "message": safe_report_text(exc)})
                                except Exception as exc:
                                    outcome["runner_failure"] = {"type": type(exc).__name__,
                                                                  "message": safe_report_text(exc)}
                                    phase_data["runner_failures"].append({"task_id": task["task_id"],
                                        "type": type(exc).__name__, "message": safe_report_text(exc)})
                                    if outcome["agent_call_started"]:
                                        phase_data["token_values_unknown"] += 1
                                    outcome.setdefault("task", {"status": "runner_error", "failure": safe_report_text(exc)})
                                    outcome.setdefault("oracle", None)
                                phase_data["task_outcomes"].append(outcome)
                                checkpoint()
                                # 每个训练任务结束后等待后台自动演化队列空闲，避免跨任务族竞态覆盖
                                await instance.wait_idle()
                                try:
                                    outcome["active_meta_after_idle"] = await instance.request(
                                        "GET", "/api/evolution/active-meta")
                                except Exception as exc:
                                    outcome["active_meta_observation_failure"] = {
                                        "type": type(exc).__name__, "message": safe_report_text(exc)}
                                checkpoint()
                                budget_state = token_budget.snapshot()
                                if budget_state["stop_reason"]:
                                    from evocode.token_budget import TokenBudgetExceeded
                                    raise TokenBudgetExceeded(budget_state)
                            await instance.wait_idle()
                            all_runs = rows_from_response(await instance.request("GET", "/api/evolution"))
                            runs = [run for run in all_runs if run.get("run_id") not in observed_run_ids]
                            observed_run_ids = {run.get("run_id") for run in all_runs}
                            versions = rows_from_response(await instance.request("GET", "/api/skills/versions"))
                            active_meta = await instance.request("GET", "/api/evolution/active-meta")
                            phase_data["evolution_runs"] = [summarize_run(run) for run in runs]
                            phase_data["task_skill_runs"] = [summarize_run(run) for run in runs
                                                              if run.get("kind") == "task"]
                            phase_data["meta_runs"] = [summarize_run(run) for run in runs
                                                       if run.get("kind") == "meta"]
                            phase_data["active_meta_at_boundary"] = active_meta
                            phase_data["skill_versions"] = [{key: version.get(key) for key in
                                ("skill_id", "version_id", "name", "level", "content_sha256", "status", "scope_id")
                                if key in version} for version in versions]
                            phase_data["lifecycle_costs"] = lifecycle_tokens(runs)
                            phase_data["proposal_reliability"] = proposal_reliability(runs)
                            if phase == "phase_1":
                                lifecycle[branch]["phase_1_boundary"] = {
                                    "active_meta": active_meta,
                                    "runs": [summarize_run(run) for run in all_runs],
                                    "task_skill_runs": [summarize_run(run) for run in all_runs
                                                        if run.get("kind") == "task"],
                                    "meta_runs": [summarize_run(run) for run in all_runs
                                                  if run.get("kind") == "meta"],
                                    "task_skill_links": task_skill_links(phase_data)}
                        lifecycle[branch]["thresholds"] = thresholds[branch]
                        lifecycle[branch]["phase_2_task_skill_links"] = task_skill_links(
                            phase_reports[branch]["phase_2"])
                        snapshot_path = root / "snapshots" / branch
                        await instance.snapshot(snapshot_path)
                        branch_snapshots[branch] = snapshot_path
                except Exception as exc:
                    from evocode.token_budget import TokenBudgetExceeded
                    if isinstance(exc, TokenBudgetExceeded):
                        raise
                    lifecycle[branch]["setup_failure"] = {
                        "type": type(exc).__name__, "message": safe_report_text(exc),
                    }
                    checkpoint()

            for branch in branches:
                if branch not in branch_snapshots:
                    for (row_branch, _), outcome in heldout_by_key.items():
                        if row_branch == branch:
                            outcome["runner_failure"] = lifecycle[branch]["setup_failure"]
                    continue
                for family, splits in manifest["families"].items():
                    for task_id in splits["held_out"]:
                        task = held[task_id]
                        run_root = root / "heldout" / branch / task_id
                        outcome = heldout_by_key[(branch, task_id)]
                        outcome["runner_failure"] = None
                        outcome["task"] = {"status": "pending"}
                        outcome["oracle"] = None
                        outcome["agent_call_started"] = False
                        try:
                            workspace = run_root / "workspace"
                            make_repo(workspace, task_id, task["source"], task_id)
                            clone_dir = run_root / "state"
                            blackbox_runtime.clone_state(branch_snapshots[branch], clone_dir)
                            outcome["workspace_commit"] = subprocess.check_output(
                                ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True).strip()
                            config = copy.deepcopy(base_config)
                            config.data_dir = str(clone_dir)
                            config.evolution.enabled = False
                            config.evolution.auto_trigger_enabled = False
                            config.evolution.auto_evaluate = False
                            config.evolution.auto_publish = False
                            config.evolution.freeze_meta_skill = branches[branch]["freeze_meta_skill"]
                            config.evolution.selection_task_specs = []
                            config.skills.enabled = True
                            config.raw_rag.enabled = False
                            config.raw_rag.index_when_disabled = False
                            config.raw_rag.background_indexing = False
                            config.runtime.execution_backend = "docker"
                            config.runtime.max_turns = min(config.runtime.max_turns, max_turns)
                            async with blackbox_runtime.Instance(
                                config, token_budget_db=token_budget_db, max_total_tokens=max_total_tokens
                            ) as instance:
                                outcome["agent_call_started"] = True
                                task_result = await instance.run_task(
                                    workspace, task["prompt"], repository_id(family)
                                )
                                outcome.update({"task": compact(task_result),
                                                "trace": task_result.get("trace", {}),
                                                "trace_artifacts": task_result.get("trace_artifacts", [])})
                                if task_result["status"] == "completed":
                                    oracle = await blackbox_runtime.protected_acceptance(
                                        config, workspace, run_root / "oracle", task["acceptance_file"],
                                        task["acceptance"], task["acceptance_command"],
                                    )
                                    outcome["oracle"] = acceptance_summary(oracle)
                                    if oracle.get("passed") is False and oracle.get("acceptance_protected") is True:
                                        outcome["oracle_failure_workspace"] = workspace_diagnostic(workspace)
                            phase_1_links = lifecycle[branch].get("phase_1_boundary", {}).get(
                                "task_skill_links", [])
                            phase_2_links = lifecycle[branch].get("phase_2_task_skill_links", [])
                            loaded_by_phase = loaded_task_skill_links_by_phase(
                                outcome["task"].get("coder_skill_injection_evidence"),
                                phase_1_links, phase_2_links, family=family)
                            outcome["loaded_phase_1_task_skill_links"] = loaded_by_phase["phase_1"]
                            outcome["loaded_phase_2_task_skill_links"] = loaded_by_phase["phase_2"]
                            outcome["loaded_task_skill_links"] = [
                                *loaded_by_phase["phase_1"], *loaded_by_phase["phase_2"]]
                            outcome["used_published_task_skill"] = bool(
                                outcome["loaded_task_skill_links"])
                            # Keep the old field for consumers of v2/v3 report shape.
                            outcome["used_published_phase_2_task_skill"] = bool(
                                loaded_by_phase["phase_2"])
                            trace_splits[branch]["held_out"].append(task_id)
                        except task_timeout_error as exc:
                            outcome["agent_timeout"] = {"type": type(exc).__name__, "message": safe_report_text(exc)}
                            if getattr(exc, "observations", None):
                                outcome["trace"] = exc.observations.get("trace", {})
                                partial = compact({"telemetry": exc.observations.get("telemetry", {})})
                                outcome["partial_agent_observation"] = {
                                    "coder_skill_hashes": partial["coder_skill_hashes"],
                                    "coder_skill_versions": partial["coder_skill_versions"],
                                    "coder_skill_contexts": partial["coder_skill_contexts"],
                                    "coder_skill_injection_evidence": partial["coder_skill_injection_evidence"]}
                                outcome["trace_artifacts"] = exc.observations.get("trace_artifacts", [])
                            outcome.update({"task": {"status": "timed_out", "failure": safe_report_text(exc)},
                                            "oracle": None})
                        except Exception as exc:  # Preserve runner failures independently of task results.
                            outcome["runner_failure"] = {"type": type(exc).__name__, "message": safe_report_text(exc)}
                            if outcome["task"]["status"] == "pending":
                                outcome["task"] = {"status": "runner_error", "failure": safe_report_text(exc)}
                        oracle = outcome.get("oracle") or {}
                        actual_hash = (oracle.get("acceptance_file_sha256") or {}).get(task["acceptance_file"])
                        oracle_verified = (oracle.get("acceptance_protected") is True and
                                           actual_hash == outcome["protected_acceptance_sha256"])
                        outcome["oracle_class"] = ("runner_failure" if outcome["runner_failure"] else
                            "agent_timeout" if outcome.get("agent_timeout") else
                            "agent_noncompletion" if outcome["task"]["status"] != "completed" else
                            "unverified" if not oracle_verified else
                            "pass" if oracle.get("passed") is True else "assertion_fail"
                        )
                        outcome["solved"] = outcome["oracle_class"] == "pass"
                        checkpoint()
                        budget_state = token_budget.snapshot()
                        if budget_state["stop_reason"]:
                            from evocode.token_budget import TokenBudgetExceeded
                            raise TokenBudgetExceeded(budget_state)
            expected = sum(len(splits["held_out"]) for splits in manifest["families"].values()) * len(branches)
            complete_matrix = len(heldout_rows) == expected and all(
                row["oracle_class"] not in {"runner_failure", "unverified"} for row in heldout_rows
            )
            phase_tasks_ok = all(
                "setup_failure" not in lifecycle[branch] and
                not phase_reports[branch][phase]["runner_failures"] and
                all(row.get("task", {}).get("status") == "completed" and
                    row.get("oracle", {}).get("passed") is True and
                    row.get("oracle", {}).get("acceptance_protected") is True
                    for row in phase_reports[branch][phase]["task_outcomes"])
                for branch in branches for phase in ("phase_1", "phase_2"))
            paired_task_ids = {row["task_id"] for row in heldout_rows}
            pairwise_comparisons = {}
            for left_branch, right_branch in combinations(branches, 2):
                pair = {f"{left_branch}_wins": 0, f"{right_branch}_wins": 0,
                        "ties": 0, "unscored_pairs": 0, "scored_pairs": 0}
                for task_id in paired_task_ids:
                    left_row = heldout_by_key.get((left_branch, task_id))
                    right_row = heldout_by_key.get((right_branch, task_id))
                    if (left_row is None or right_row is None or left_row.get("runner_failure") or
                            right_row.get("runner_failure") or left_row.get("oracle_class") == "unverified" or
                            right_row.get("oracle_class") == "unverified"):
                        pair["unscored_pairs"] += 1
                        continue
                    left, right = left_row.get("solved") is True, right_row.get("solved") is True
                    pair["scored_pairs"] += 1
                    if left == right:
                        pair["ties"] += 1
                    elif left:
                        pair[f"{left_branch}_wins"] += 1
                    else:
                        pair[f"{right_branch}_wins"] += 1
                pairwise_comparisons[f"{left_branch}_vs_{right_branch}"] = pair
            pass_rates = {branch: {
                "attempted": sum(row["branch"] == branch and row.get("agent_call_started") is True
                                  for row in heldout_rows),
                "solved": sum(row["branch"] == branch and row.get("solved") is True for row in heldout_rows),
                "oracle_unverified": sum(row["branch"] == branch and row.get("oracle_class") == "unverified"
                                          for row in heldout_rows),
                "oracle_assertion_failures": sum(row["branch"] == branch and row.get("oracle_class") == "assertion_fail"
                                                 for row in heldout_rows),
                "runner_failures": sum(row["branch"] == branch and row.get("oracle_class") == "runner_failure"
                                        for row in heldout_rows),
                "agent_timeouts": sum(row["branch"] == branch and row.get("oracle_class") == "agent_timeout"
                                      for row in heldout_rows),
                "agent_noncompletions": sum(row["branch"] == branch and row.get("oracle_class")
                                            in {"agent_noncompletion", "agent_timeout"} for row in heldout_rows),
            } for branch in branches}
            heldout_costs = {branch: {"planned_tasks": sum(len(s["held_out"])
                                                            for s in manifest["families"].values()),
                                      "agent_attempts": 0, "agent_results_observed": 0,
                                      "agent_timeouts": 0, "reported_tokens": 0,
                                      "token_values_unknown": 0}
                             for branch in branches}
            for row in heldout_rows:
                task_result = row.get("task") or {}
                if row.get("agent_call_started"):
                    heldout_costs[row["branch"]]["agent_attempts"] += 1
                if task_result.get("status") == "timed_out":
                    heldout_costs[row["branch"]]["agent_timeouts"] += 1
                    heldout_costs[row["branch"]]["token_values_unknown"] += 1
                elif task_result.get("status") == "runner_error" and row.get("agent_call_started"):
                    heldout_costs[row["branch"]]["token_values_unknown"] += 1
                elif task_result.get("status") != "runner_error":
                    heldout_costs[row["branch"]]["agent_results_observed"] += 1
                    tokens = task_result.get("tokens")
                    if isinstance(tokens, (int, float)):
                        heldout_costs[row["branch"]]["reported_tokens"] += int(tokens)
                    else:
                        heldout_costs[row["branch"]]["token_values_unknown"] += 1
            heldout_runner_failures = sum(bool(row.get("runner_failure")) for row in heldout_rows)
            mechanism_gates = {}
            for branch in branches:
                task_runs = [run for phase in ("phase_1", "phase_2")
                             for run in phase_reports[branch][phase].get("task_skill_runs", [])]
                meta_runs = [run for phase in ("phase_1", "phase_2")
                             for run in phase_reports[branch][phase].get("meta_runs", [])]
                links = [*lifecycle[branch].get("phase_1_boundary", {}).get("task_skill_links", []),
                         *lifecycle[branch].get("phase_2_task_skill_links", [])]
                lifecycle[branch]["all_task_skill_links"] = links
                loaded_rows = [row for row in heldout_rows if row["branch"] == branch]
                heldout_skill_usage = {
                    "loaded_phase_1_task_skill_links": sum(
                        len(row.get("loaded_phase_1_task_skill_links", [])) for row in loaded_rows),
                    "loaded_phase_2_task_skill_links": sum(
                        len(row.get("loaded_phase_2_task_skill_links", [])) for row in loaded_rows),
                    "total_loaded_published_task_skill_links": sum(
                        len(row.get("loaded_task_skill_links", [])) for row in loaded_rows),
                    "heldout_rows_using_published_task_skill": sum(
                        row.get("used_published_task_skill") is True for row in loaded_rows),
                }
                loaded_by_family = {
                    family_key: any(row.get("family") == family_key and
                                    any(item.get("family") == family_key for item in
                                        row.get("loaded_task_skill_links", []))
                                    for row in loaded_rows)
                    for family_key in manifest["families"]
                }
                verified_injection_by_family = {
                    family_key: [item for row in loaded_rows if row.get("family") == family_key
                                 for item in row.get("loaded_task_skill_links", [])
                                 if item.get("family") == family_key and
                                 item.get("injection_verified") is True and
                                 item.get("model_call_observed") is True]
                    for family_key in manifest["families"]
                }
                activity = lifecycle_activity(task_runs, meta_runs)
                published_by_family = {
                    family_key: any(link.get("family") == family_key and
                                    link.get("status") == "published" and link.get("published_version")
                                    for link in links)
                    for family_key in manifest["families"]
                }
                initial_meta = lifecycle[branch].get("initial_active_meta") or {}
                final_meta = phase_reports[branch]["phase_2"].get("active_meta_at_boundary") or {}
                meta_changed = ((initial_meta.get("version_id"), initial_meta.get("content_sha256")) !=
                                (final_meta.get("version_id"), final_meta.get("content_sha256")))
                if branch == "no_learning":
                    initial_ids = {item.get("version_id") for item in
                                   lifecycle[branch].get("initial_skill_versions", [])}
                    final_ids = {item.get("version_id") for item in
                                 phase_reports[branch]["phase_2"].get("skill_versions", [])}
                    new_task_versions = [item.get("version_id") for item in
                                         phase_reports[branch]["phase_2"].get("skill_versions", [])
                                         if item.get("level") == "task" and
                                         item.get("version_id") not in initial_ids]
                    mechanism_gates[branch] = {
                        "passed": (not thresholds[branch]["evolution_enabled"] and not task_runs and
                                   not meta_runs and not (new_task_versions or (final_ids - initial_ids))),
                        "evolution_disabled": not thresholds[branch]["evolution_enabled"],
                        "task_skill_runs": len(task_runs), "meta_skill_runs": len(meta_runs),
                        "new_task_skill_versions": new_task_versions,
                        "mechanism_proposal_and_evaluation_activity": activity,
                        "candidate_decisions": activity["candidate_decisions"],
                    }
                elif branch == "task_skill_only":
                    active_meta_initialization_stable = not meta_changed
                    task_skill_training_and_use = (any(run.get("status") == "published" for run in task_runs) and
                                                   all(published_by_family.values()) and
                                                   all(loaded_by_family.values()))
                    mechanism_gates[branch] = {
                        "passed": active_meta_initialization_stable and task_skill_training_and_use,
                        "meta_unchanged": active_meta_initialization_stable,
                        "published_task_skill_by_family": published_by_family,
                        "published_task_skill_loaded_in_heldout_by_family": loaded_by_family,
                        "heldout_published_task_skill_usage": heldout_skill_usage,
                        "task_skill_injection_evidence_by_family": verified_injection_by_family,
                        "mechanism_proposal_and_evaluation_activity": activity,
                        "candidate_decisions": activity["candidate_decisions"],
                        "task_skill_runs": len(task_runs), "meta_skill_runs": len(meta_runs),
                    }
                else:
                    changed_meta_source_links = [link for link in links
                                                 if link.get("source_meta_matches_active_before") is True and
                                                 (link.get("source_meta_skill_version_id"),
                                                  link.get("source_meta_content_sha256")) !=
                                                 (initial_meta.get("version_id"),
                                                  initial_meta.get("content_sha256"))]
                    changed_meta_links = [link for link in changed_meta_source_links
                                          if link.get("status") == "published" and
                                          link.get("published_version")]
                    loaded_changed_meta_link = any(
                        row.get("family") == link.get("family") and
                        any(item.get("published_version") == link.get("published_version") and
                            item.get("published_content_sha256") == link.get("published_content_sha256") and
                            item.get("coder_loaded_version_id") == link.get("published_version") and
                            item.get("coder_loaded_content_sha256") == link.get("published_content_sha256") and
                            item.get("injection_verified") is True and
                            item.get("model_call_observed") is True
                            for item in row.get("loaded_task_skill_links", []))
                        for link in changed_meta_links for row in loaded_rows)
                    mechanism_gates[branch] = {
                        "passed": meta_changed and bool(changed_meta_links) and loaded_changed_meta_link,
                        "meta_changed": meta_changed,
                        "meta_changed_source_matched": bool(changed_meta_source_links),
                        "downstream_task_skill_published": bool(changed_meta_links),
                        "downstream_task_skill_loaded_with_verified_injection": loaded_changed_meta_link,
                        "published_task_skills_based_on_changed_meta": changed_meta_links,
                        "changed_meta_task_skill_loaded_in_heldout": loaded_changed_meta_link,
                        "task_skill_injection_evidence_by_family": verified_injection_by_family,
                        "heldout_published_task_skill_usage": heldout_skill_usage,
                        "mechanism_proposal_and_evaluation_activity": activity,
                        "candidate_decisions": activity["candidate_decisions"],
                        "task_skill_runs": len(task_runs), "meta_skill_runs": len(meta_runs),
                    }
            initial_skill_snapshots = {
                branch: sorted((item.get("version_id"), item.get("content_sha256"))
                               for item in lifecycle[branch].get("initial_skill_versions", []))
                for branch in branches
            }
            same_initial_skill_snapshot = len({json.dumps(value, sort_keys=True)
                                               for value in initial_skill_snapshots.values()}) == 1
            mechanism_gate_passed = (all(gate.get("passed") is True for gate in mechanism_gates.values())
                                     and same_initial_skill_snapshot)
            token_budget_snapshot = token_budget.snapshot()
            report.update({"status": "completed" if complete_matrix else "failed",
                           "trace_splits": trace_splits,
                           "phases": phase_reports,
                           "lifecycle": lifecycle,
                           "thresholds": thresholds,
                           "phase_2_learning_trigger_diagnostic": phase_2_trigger_diagnostic(
                               manifest,
                               batch_task_count=thresholds["task_skill_only"]["batch_task_count"],
                               similar_issue_threshold=thresholds["task_skill_only"]["similar_issue_threshold"]),
                           "token_budget": token_budget_snapshot,
                           "proposal_reliability": {
                               branch: {phase: phase_reports[branch][phase].get("proposal_reliability", {})
                                        for phase in ("phase_1", "phase_2")}
                               for branch in branches},
                           "costs": {phase: {branch: {
                               "agent_reported_tokens": phase_reports[branch][phase]["reported_tokens"],
                               "agent_token_values_unknown": phase_reports[branch][phase]["token_values_unknown"],
                               "agent_attempts": phase_reports[branch][phase]["attempted_tasks"],
                               "agent_results_observed": phase_reports[branch][phase]["task_count"],
                               **phase_reports[branch][phase].get("lifecycle_costs", {})}
                               for branch in branches} for phase in ("phase_1", "phase_2")} |
                               {"heldout": heldout_costs},
                           "heldout_diagnostic": {"status": "completed" if complete_matrix else "partial",
                                                  "pass_rates": pass_rates,
                                                  "pairwise_comparisons": pairwise_comparisons,
                                                  "planned_outcomes": expected,
                                                  "accounted_outcomes": len(heldout_rows),
                                                  "runner_failures": heldout_runner_failures,
                                                  "paired_outcomes": heldout_rows},
                           "gates": {"all_phase_tasks_completed_with_protected_acceptance": phase_tasks_ok,
                               "heldout_matrix_complete": complete_matrix,
                               "heldout_runner_failures": heldout_runner_failures,
                               "heldout_oracles_protected": all(
                                   row.get("oracle_class") in {"pass", "assertion_fail"}
                                   for row in heldout_rows if row["task"]["status"] == "completed"),
                               "raw_rag_disabled": True,
                               "selection_oracle_paths_redacted_from_config_api": True,
                               "autonomous_publication_enabled": True,
                               "manual_evolution_actions_used": False,
                               "mechanism_gates": mechanism_gates,
                               "same_initial_skill_snapshot_across_arms": same_initial_skill_snapshot,
                               "prevalidation_mechanisms_passed": mechanism_gate_passed,
                               "large_benchmark_ready": (complete_matrix and phase_tasks_ok and
                                                         mechanism_gate_passed and
                                                         heldout_runner_failures == 0 and
                                                         token_budget_snapshot["usage_unknown_requests"] == 0)}})
        except Exception as exc:  # keep the actual failure boundary in the report
            token_budget_snapshot = token_budget.snapshot()
            from evocode.token_budget import TokenBudgetExceeded
            budget_exhausted = isinstance(exc, TokenBudgetExceeded) or bool(token_budget_snapshot["stop_reason"])
            for row in heldout_rows:
                if (row.get("runner_failure") or {}).get("type") == "not_attempted":
                    row["runner_failure"] = {"type": "not_attempted",
                                              "message": f"Earlier runner failure: {type(exc).__name__}: {exc}"}
                    row["task"] = {"status": "runner_error", "failure": safe_report_text(exc)}
            report.update({"status": "budget_exhausted" if budget_exhausted else "failed",
                           "error_type": type(exc).__name__,
                           "error": safe_report_text(exc), "trace_splits": trace_splits,
                           "phases": phase_reports,
                           "lifecycle": lifecycle,
                           "token_budget": token_budget_snapshot,
                           "large_benchmark_ready": False,
                           "heldout_diagnostic": {
                               "status": "partial",
                               "planned_outcomes": len(heldout_rows),
                               "accounted_outcomes": len(heldout_rows),
                               "runner_failures": sum(bool(row.get("runner_failure"))
                                                       for row in heldout_rows),
                               "paired_outcomes": heldout_rows}})
            checkpoint()
        else:
            persist_report(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("preflight", "live"), default="preflight")
    parser.add_argument("--output", type=Path, help="Optional external report path; never overwritten")
    parser.add_argument("--retain-dir", type=Path,
                        help="Optional directory for retained live workspaces, artifacts, and state")
    parser.add_argument("--max-turns", type=int, default=40,
                        help="Cap runtime turns per task (1–40); does not change evolution thresholds")
    parser.add_argument("--max-total-tokens", type=int, default=1_500_000,
                        help="Shared hard admission budget for all model calls in live mode")
    parser.add_argument("--family", help="Limit execution to a manifest family key")
    parser.add_argument("--fixture-dir", type=Path,
                        help="Use an alternate RSI fixture directory (default: tests/fixtures/prevalidation/rsi)")
    args = parser.parse_args()
    if args.max_turns < 1 or args.max_turns > 40:
        parser.error("--max-turns must be between 1 and 40")
    if args.max_total_tokens < 1:
        parser.error("--max-total-tokens must be positive")
    report_path = args.output.expanduser().resolve() if args.output else None
    if report_path is not None and report_path.exists():
        parser.error(f"Refusing to overwrite existing report: {report_path}")
    if args.phase == "preflight":
        result = preflight(args.family, args.fixture_dir)
    else:
        result = asyncio.run(live(family=args.family, max_turns=args.max_turns,
                                  max_total_tokens=args.max_total_tokens, retain_dir=args.retain_dir,
                                  report_path=report_path, fixture_dir=args.fixture_dir))
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if report_path is not None and args.phase == "preflight":
        path = report_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
    print(output)
    if result.get("status") not in ("preflight_passed", "completed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
