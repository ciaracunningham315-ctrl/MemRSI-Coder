"""Qualify independent repo_cli publication and regression-replay fixtures offline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.dataset_validation import copy_clean, execute, inventory, passed, completed, source_path

TASK_IDS = (
    "repo_cli_gate_seed_bundle",
    "repo_cli_gate_transfer_cache",
    "repo_cli_gate_stale_release",
    "repo_cli_gate_meta_transfer_manifest",
    "repo_cli_replay_profile_summary",
)


def manifest_path(task_id: str) -> Path:
    family = "regression_replay" if task_id == "repo_cli_replay_profile_summary" else "gate_selection"
    return ROOT / "private" / family / "repo_cli" / task_id / "manifest.json"


def expected_fingerprints(manifest: dict) -> dict:
    values = {}
    for key in ("code_dir", "private_tests_dir", "regression_tests_dir", "reference_dir"):
        values[key] = inventory(source_path(ROOT, manifest[key]))
    task = source_path(ROOT, manifest["task_file"])
    values["task_file"] = hashlib.sha256(task.read_bytes()).hexdigest()
    return values


def validate_one(task_id: str, work: Path, python: str) -> dict:
    path = manifest_path(task_id)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected_kind = "regression" if "replay" in task_id else ("meta" if "_meta_" in task_id else "task")
    if manifest.get("task_id") != task_id or manifest.get("repository_id") != "repo_cli":
        raise ValueError(f"identity mismatch for {task_id}")
    if manifest.get("kind") != expected_kind:
        raise ValueError(f"kind mismatch for {task_id}")
    if manifest.get("stage") not in ("seed", "transfer", "stale"):
        raise ValueError(f"invalid stage for {task_id}")
    if manifest.get("available_after_stage") != manifest.get("stage") or manifest.get("latest_mechanism_stage") != manifest.get("stage"):
        raise ValueError(f"stage provenance mismatch for {task_id}")
    if manifest.get("independent_of_main_tasks") is not True or manifest.get("future_main_inputs_used") != []:
        raise ValueError(f"task is not isolated from main/future inputs: {task_id}")
    trace = manifest.get("mechanism_trace", {})
    if (trace.get("topic") != "configuration_source_precedence_and_parameter_bounds"
            or trace.get("stage_boundary") != manifest.get("stage")
            or trace.get("future_main_inputs_used") != []
            or trace.get("fixture_origin") != "independently authored repo_cli gate data"):
        raise ValueError(f"mechanism provenance missing or inconsistent for {task_id}")
    if expected_kind == "meta" and manifest.get("stage") != "transfer":
        raise ValueError("Meta Gate must sit at the transfer review boundary")
    if expected_kind == "meta":
        evidence = manifest.get("downstream_injection_check", {})
        required = {"old_meta_version", "candidate_meta_version", "derived_skill_sha256",
                    "downstream_request_id", "injected_skill_version_and_hash"}
        if evidence.get("required") is not True or not required.issubset(set(evidence.get("required_evidence", []))):
            raise ValueError("Meta Gate must declare candidate-specific downstream injection evidence")
    if manifest.get("acceptance_command") != ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]:
        raise ValueError(f"unsupported command for {task_id}")
    for key in ("code_dir", "task_file", "private_tests_dir", "regression_tests_dir", "reference_dir"):
        resolved = source_path(ROOT, manifest[key])
        if not resolved.is_relative_to(path.parent):
            raise ValueError(f"{task_id} source escapes independent task directory: {key}")
    fingerprints = expected_fingerprints(manifest)
    if fingerprints != manifest.get("fingerprints"):
        raise ValueError(f"stale fingerprints for {task_id}")

    task_dir = path.parent
    log_dir = task_dir / "qualification"
    log_dir.mkdir(parents=True, exist_ok=True)
    commands: dict[str, dict] = {}
    for variant, code_key in (("baseline", "code_dir"), ("reference", "reference_dir")):
        suites: dict[str, dict] = {}
        for suite in ("public", "target", "regression", "combined"):
            checkout = work / task_id / variant / suite
            copy_clean(source_path(ROOT, manifest[code_key]), checkout)
            if suite in ("target", "regression", "combined"):
                keys = (("private_tests_dir",) if suite == "target" else
                        ("regression_tests_dir",) if suite == "regression" else
                        ("private_tests_dir", "regression_tests_dir"))
                for key in keys:
                    source = source_path(ROOT, manifest[key])
                    for relative in inventory(source):
                        destination = checkout / "tests" / relative
                        if destination.exists():
                            raise ValueError(f"oracle filename collision in {task_id}: {relative}")
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes((source / relative).read_bytes())
            command = [python, *manifest["acceptance_command"][1:]]
            suites[suite] = execute(command, checkout, log_dir / f"{variant}_{suite}.log")
        commands[variant] = suites

    baseline = commands["baseline"]
    reference = commands["reference"]
    checks = {
        "baseline_public_passes": passed(baseline["public"]),
        "baseline_target_fails": (completed(baseline["target"]) and baseline["target"]["exit_code"] != 0
                                  and baseline["target"]["test_failures"] > 0 and baseline["target"]["test_errors"] == 0),
        "baseline_regression_passes": passed(baseline["regression"]),
        "reference_all_passes": all(passed(result) for result in reference.values()),
    }
    record = {
        "task_id": task_id,
        "repository_id": "repo_cli",
        "stage": manifest["stage"],
        "kind": expected_kind,
        "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "fingerprints": fingerprints,
        "checks": checks,
        "passed": all(checks.values()),
        "commands": commands,
        "model_calls": 0,
        "formal_results": False,
    }
    (log_dir / "evidence.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def validate(python: str = sys.executable) -> dict:
    work = ROOT / ".runtime" / ("repo_cli_gate_validation_" + uuid.uuid4().hex)
    work.mkdir(parents=True, exist_ok=True)
    records = []
    for task_id in TASK_IDS:
        try:
            records.append(validate_one(task_id, work, python))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            records.append({"task_id": task_id, "passed": False, "error": str(exc)})
    return {
        "kind": "repo_cli_independent_gate_and_replay_offline_admission",
        "passed": all(record.get("passed") is True for record in records),
        "model_calls": 0,
        "formal_results": False,
        "tasks": records,
        "work_dir": str(work.relative_to(ROOT)),
        "boundary": "Fixture executability and oracle isolation only; no model, Skill benefit, Meta benefit, or formal benchmark result.",
    }


def markdown(report: dict) -> str:
    lines = [
        "# repo_cli 独立 Gate 与回归重放准入",
        "",
        "本报告仅验证离线夹具可执行性、故障隔离与参考修复；模型调用为 0，不代表模型效果或正式结果。",
        "",
        f"总体准入：{'通过' if report['passed'] else '未通过'}。",
        "",
        "| 任务 ID | 阶段 | 类型 | 公开检查 | 目标验收失败 | 基线回归 | 参考修复 |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for task in report["tasks"]:
        checks = task.get("checks", {})
        lines.append(
            f"| {task['task_id']} | {task.get('stage', '')} | {task.get('kind', '')} | "
            f"{checks.get('baseline_public_passes')} | {checks.get('baseline_target_fails')} | "
            f"{checks.get('baseline_regression_passes')} | {checks.get('reference_all_passes')} |"
        )
    lines += ["", f"独立工作副本和命令日志：`{report['work_dir']}`。", ""]
    return "\n".join(lines)


def main() -> int:
    report = validate()
    target = ROOT / "reports" / "repo_cli_gate_validation.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target.with_suffix(".md")).write_text(markdown(report), encoding="utf-8")
    print("repo_cli independent Gate/replay qualification:", "PASS" if report["passed"] else "FAIL")
    print(target)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
