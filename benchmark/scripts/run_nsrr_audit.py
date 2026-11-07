#!/usr/bin/env python3
"""Run the frozen, model-free NSRR synthetic audit.

This runner intentionally reads only gate_cases/<NSRR id>/ and private/gate_audit/.
The policy shim is a deterministic fixture for exercising the audit and label
pipeline; it is not an LLM, the Gate, or evidence of real Gate effectiveness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BENCHMARK_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BENCHMARK_ROOT.parent
CASES_ROOT = BENCHMARK_ROOT / "gate_cases"
AUDIT_ROOT = BENCHMARK_ROOT / "private" / "gate_audit"
TASKS_ROOT = AUDIT_ROOT / "tasks"
REPORT_JSON = BENCHMARK_ROOT / "reports" / "nsrr_audit_offline.json"
REPORT_MD = BENCHMARK_ROOT / "reports" / "nsrr_audit_offline.md"
LABELS_PATH = AUDIT_ROOT / "observed_labels.json"
FREEZE_PATH = AUDIT_ROOT / "freeze_manifest.json"
RUBRIC_PATH = AUDIT_ROOT / "rubric.json"

if str(BENCHMARK_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_ROOT))

from runner.nsrr_audit_tasks import nsrr_fixture_input_paths  # noqa: E402

AUDIT_VERSION = "nsrr-independent-synthetic-audit-v1"
RUBRIC_ID = "nsrr-observation-rules-v1"
ALLOWED_TYPES = {
    "duplicate",
    "stale",
    "potentially_harmful",
    "potentially_beneficial",
}
ALLOWED_LABELS = {"harmful", "neutral", "beneficial", "unknown"}

EXPECTED_CANDIDATES = {
    "nsrr_dup_headers": "duplicate",
    "nsrr_dup_config_null": "duplicate",
    "nsrr_stale_timezone": "stale",
    "nsrr_stale_retry": "stale",
    "nsrr_harm_idempotency": "potentially_harmful",
    "nsrr_harm_error_swallow": "potentially_harmful",
    "nsrr_benefit_decimal": "potentially_beneficial",
    "nsrr_benefit_cache": "potentially_beneficial",
}


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _relative(path: Path) -> str:
    return path.relative_to(BENCHMARK_ROOT).as_posix()


def candidate_metadata_paths() -> list[Path]:
    return [CASES_ROOT / candidate_id / "candidate.json" for candidate_id in EXPECTED_CANDIDATES]


def _candidate_records() -> list[dict[str, Any]]:
    records = []
    for metadata_path in candidate_metadata_paths():
        if not metadata_path.is_file():
            raise FileNotFoundError(f"missing NSRR candidate metadata: {_relative(metadata_path)}")
        record = _load_json(metadata_path)
        candidate_id = record.get("candidate_id")
        if candidate_id not in EXPECTED_CANDIDATES:
            raise ValueError(f"unexpected candidate_id: {candidate_id!r}")
        if record.get("candidate_type") != EXPECTED_CANDIDATES[candidate_id]:
            raise ValueError(f"candidate type mismatch for {candidate_id}")
        if record.get("audit_task_id") != candidate_id.replace("nsrr_", "nsrr_audit_", 1):
            raise ValueError(f"audit task linkage mismatch for {candidate_id}")
        for field in ("baseline_skill", "candidate_skill", "baseline_policy", "candidate_policy"):
            rel = record.get(field)
            if not isinstance(rel, str) or not rel.startswith(f"gate_cases/{candidate_id}/"):
                raise ValueError(f"{candidate_id}.{field} must stay inside its candidate directory")
            if not (BENCHMARK_ROOT / rel).is_file():
                raise FileNotFoundError(f"missing {field}: {rel}")
        task_id = record["audit_task_id"]
        task_dir = TASKS_ROOT / task_id
        if not (task_dir / "task.json").is_file() or not (task_dir / "trusted_tests.json").is_file():
            raise FileNotFoundError(f"incomplete independent audit task: {task_id}")
        records.append(record)
    if len(records) != 8 or {r["candidate_id"] for r in records} != set(EXPECTED_CANDIDATES):
        raise ValueError("NSRR candidate inventory must contain exactly the eight frozen IDs")
    counts = {kind: sum(r["candidate_type"] == kind for r in records) for kind in ALLOWED_TYPES}
    required = {"duplicate": 2, "stale": 2, "potentially_harmful": 2, "potentially_beneficial": 2}
    if counts != required:
        raise ValueError(f"candidate role counts mismatch: {counts}")
    return records


def _input_paths(records: list[dict[str, Any]]) -> list[Path]:
    paths: set[Path] = {
        RUBRIC_PATH,
        Path(__file__).resolve(),
        BENCHMARK_ROOT / "tests" / "test_nsrr_audit.py",
        BENCHMARK_ROOT / "tests" / "test_nsrr_audit_fixture_qualification.py",
    }
    for record in records:
        case_dir = CASES_ROOT / record["candidate_id"]
        paths.add(case_dir / "candidate.json")
        for field in ("baseline_skill", "candidate_skill", "baseline_policy", "candidate_policy"):
            paths.add(BENCHMARK_ROOT / record[field])
        task_dir = TASKS_ROOT / record["audit_task_id"]
        paths.update({task_dir / "task.json", task_dir / "trusted_tests.json"})
    paths.update(nsrr_fixture_input_paths(BENCHMARK_ROOT).values())
    missing = sorted(_relative(path) for path in paths if not path.is_file())
    if missing:
        raise FileNotFoundError("missing frozen NSRR inputs: " + ", ".join(missing))
    return sorted(paths, key=_relative)


def build_freeze_manifest() -> dict[str, Any]:
    records = _candidate_records()
    paths = _input_paths(records)
    return {
        "audit_version": AUDIT_VERSION,
        "rubric_id": RUBRIC_ID,
        "freeze_date": "2026-11-06",
        "scope": "independent synthetic audit fixtures only",
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "inputs": {_relative(path): _sha256_file(path) for path in paths},
    }


def write_freeze_manifest() -> dict[str, Any]:
    manifest = build_freeze_manifest()
    FREEZE_PATH.write_bytes(_canonical_json(manifest))
    return manifest


def verify_freeze_manifest() -> tuple[dict[str, Any], str]:
    if not FREEZE_PATH.is_file():
        raise FileNotFoundError("freeze_manifest.json missing; freeze inputs before running the audit")
    locked = _load_json(FREEZE_PATH)
    observed = build_freeze_manifest()
    if locked != observed:
        expected = locked.get("inputs", {})
        actual = observed.get("inputs", {})
        changed = sorted(key for key in set(expected) | set(actual) if expected.get(key) != actual.get(key))
        detail = ", ".join(changed[:12]) or "manifest header or runner fingerprint"
        raise ValueError(f"frozen NSRR inputs changed; refusing to label: {detail}")
    return locked, _sha256_file(FREEZE_PATH)


def _normalize_output(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def execute_policy(task_type: str, policy: dict[str, Any], input_data: dict[str, Any]) -> dict[str, Any]:
    mode = policy.get("mode")
    if task_type == "header_normalization":
        headers = input_data["headers"]
        output = dict(headers) if mode == "preserve_case" else {k.lower(): v for k, v in headers.items()}
        return {"headers": output}
    if task_type == "config_nulls":
        config = input_data["config"]
        if mode == "drop_nulls":
            config = {key: value for key, value in config.items() if value is not None}
        return {"config": config}
    if task_type == "timestamp_normalization":
        parsed = datetime.fromisoformat(input_data["timestamp"].replace("Z", "+00:00"))
        if mode == "assume_utc":
            parsed = parsed.replace(tzinfo=None).replace(tzinfo=timezone.utc)
        normalized = parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return {"timestamp_utc": normalized}
    if task_type == "retry_safety":
        safe_method = input_data["method"].upper() in {"GET", "HEAD", "OPTIONS"}
        keyed = bool(input_data.get("idempotency_key"))
        retry_allowed = mode == "retry_all" or safe_method or keyed
        attempts = input_data["available_attempts"] if retry_allowed else 1
        committed_attempts = input_data["committed_attempts"][:attempts]
        effects = sum(committed_attempts)
        if keyed and effects:
            effects = 1
        return {"attempts": attempts, "side_effects": effects}
    if task_type == "idempotency":
        seen: set[str] = set()
        effects = 0
        for request in input_data["requests"]:
            key = request.get("idempotency_key") if mode == "preserve_key" else None
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            effects += 1
        return {"side_effects": effects}
    if task_type == "validation_error":
        if input_data["valid"]:
            return {"status": 200, "body": input_data["body"]}
        if mode == "empty_success":
            return {"status": 200, "body": {}}
        return {"status": 400, "body": {"error": "invalid_input"}}
    if task_type == "decimal_precision":
        if mode == "float_sum":
            total = sum(float(part) for part in input_data["addends"])
            amount = repr(total)
        else:
            total = sum((Decimal(part) for part in input_data["addends"]), Decimal("0"))
            amount = format(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")
        return {"amount": amount}
    if task_type == "cache_observability":
        key = input_data["key"]
        value = input_data["metadata"][key]
        return {"value": value}
    if task_type == "timezone_lookup":
        if mode == "fixed_offset_only" or not input_data.get("zone_verified"):
            return {"status": 400, "error": "timezone_not_supported"}
        local = datetime.fromisoformat(input_data["local_time"])
        offset = input_data["resolved_offset_minutes"]
        resolved = local.replace(tzinfo=timezone.utc).timestamp() - offset * 60
        normalized = datetime.fromtimestamp(resolved, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return {"status": 200, "timestamp_utc": normalized}
    raise ValueError(f"unsupported frozen audit task_type: {task_type}")


def _observation(task: dict[str, Any], tests: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    task_type = task["task_type"]
    cases = tests.get("cases", [])
    if len(cases) < task.get("minimum_cases", 1):
        return {"status": "insufficient_cases", "passed": 0, "total": len(cases), "pass_rate": None, "failures": []}
    outcomes = []
    failures = []
    for case in cases:
        actual = _normalize_output(execute_policy(task_type, policy, case["input"]))
        expected = _normalize_output(case["expected"])
        passed = actual == expected
        outcomes.append({"test_id": case["id"], "passed": passed, "actual": actual})
        if not passed:
            failures.append({"test_id": case["id"], "expected": expected, "actual": actual})
    perf = tests.get("performance_observations")
    min_perf = task.get("minimum_performance_samples", 0)
    perf_count = len(perf) if isinstance(perf, list) else 0
    return {
        "status": "observed",
        "passed": len(cases) - len(failures),
        "total": len(cases),
        "pass_rate": round((len(cases) - len(failures)) / len(cases), 6),
        "outcomes": outcomes,
        "failures": failures,
        "performance_samples": perf_count,
        "minimum_performance_samples": min_perf,
        "performance_evidence_sufficient": perf_count >= min_perf,
    }


def classify(task: dict[str, Any], baseline: dict[str, Any], candidate: dict[str, Any]) -> tuple[str, str]:
    if baseline["status"] != "observed" or candidate["status"] != "observed":
        return "unknown", "insufficient_valid_observations"
    baseline_outcomes = {item["test_id"]: item["passed"] for item in baseline["outcomes"]}
    candidate_outcomes = {item["test_id"]: item["passed"] for item in candidate["outcomes"]}
    common = sorted(set(baseline_outcomes) & set(candidate_outcomes))
    if len(common) < task.get("minimum_cases", 1):
        return "unknown", "insufficient_paired_observations"
    regressions = [test_id for test_id in common if baseline_outcomes[test_id] and not candidate_outcomes[test_id]]
    if regressions:
        return "harmful", "candidate_regressed_independent_assertions"
    if candidate["passed"] < baseline["passed"]:
        return "harmful", "candidate_reduced_assertion_pass_count"
    if candidate["passed"] > baseline["passed"]:
        return "beneficial", "candidate_improved_assertions_without_regression"
    if baseline_outcomes != candidate_outcomes:
        return "unknown", "paired_assertions_disagree_without_strict_gain"
    if task.get("claimed_observation_dimension") == "performance" and not candidate["performance_evidence_sufficient"]:
        return "unknown", "performance_claim_lacks_minimum_measurements"
    return "neutral", "no_observed_change_on_frozen_assertions"


def run_audit() -> tuple[dict[str, Any], dict[str, Any]]:
    frozen, freeze_sha = verify_freeze_manifest()
    rubric = _load_json(RUBRIC_PATH)
    if rubric.get("rubric_id") != RUBRIC_ID:
        raise ValueError("rubric ID differs from the frozen runner contract")
    outputs = []
    for record in _candidate_records():
        candidate_id = record["candidate_id"]
        task_dir = TASKS_ROOT / record["audit_task_id"]
        task = _load_json(task_dir / "task.json")
        tests = _load_json(task_dir / "trusted_tests.json")
        baseline_policy = _load_json(BENCHMARK_ROOT / record["baseline_policy"])
        candidate_policy = _load_json(BENCHMARK_ROOT / record["candidate_policy"])
        baseline_obs = _observation(task, tests, baseline_policy)
        candidate_obs = _observation(task, tests, candidate_policy)
        label, rule_id = classify(task, baseline_obs, candidate_obs)
        if label not in ALLOWED_LABELS:
            raise ValueError(f"invalid observed label for {candidate_id}: {label}")
        version_hashes = {
            field: frozen["inputs"][record[field]]
            for field in ("baseline_skill", "candidate_skill", "baseline_policy", "candidate_policy")
        }
        version_hashes.update({
            "candidate_metadata": frozen["inputs"][_relative(CASES_ROOT / candidate_id / "candidate.json")],
            "audit_task": frozen["inputs"][_relative(task_dir / "task.json")],
            "trusted_tests": frozen["inputs"][_relative(task_dir / "trusted_tests.json")],
            "fixture_manifest": frozen["inputs"][_relative(task_dir / "fixture_manifest.json")],
            "fixture_task_file": frozen["inputs"][json.loads((task_dir / "fixture_manifest.json").read_text(encoding="utf-8"))["task_file"]],
            "fixture_input_fingerprint": json.loads((task_dir / "fixture_manifest.json").read_text(encoding="utf-8"))["fixed_input_fingerprint"],
            "rubric": frozen["inputs"][_relative(RUBRIC_PATH)],
            "runner": frozen["runner_sha256"],
            "freeze_manifest": freeze_sha,
        })
        outputs.append({
            "candidate_id": candidate_id,
            "candidate_type": record["candidate_type"],
            "audit_task_id": record["audit_task_id"],
            "observed_label": label,
            "baseline_observation": baseline_obs,
            "candidate_observation": candidate_obs,
            "rule_id": rule_id,
            "evidence_refs": [
                _relative(task_dir / "task.json"),
                _relative(task_dir / "trusted_tests.json"),
                json.loads((task_dir / "fixture_manifest.json").read_text(encoding="utf-8"))["task_file"],
                _relative(task_dir / "fixture_manifest.json"),
                _relative(BENCHMARK_ROOT / record["baseline_skill"]),
                _relative(BENCHMARK_ROOT / record["candidate_skill"]),
                _relative(BENCHMARK_ROOT / record["baseline_policy"]),
                _relative(BENCHMARK_ROOT / record["candidate_policy"]),
            ],
            "version_hashes": version_hashes,
            "source_provenance": record["source_provenance"],
            "audit_source": task["independent_source"],
            "oracle_source": tests["oracle"],
        })
    report = {
        "audit_version": AUDIT_VERSION,
        "rubric_id": RUBRIC_ID,
        "freeze_manifest_sha256": freeze_sha,
        "execution": "offline deterministic policy shim; no model or API invoked",
        "purpose": "validate the NSRR audit fixture, observation, hashing, and label pipeline only",
        "limitations": [
            "does not run or approximate an LLM Gate",
            "does not establish Gate precision, recall, NSRR performance, or model behavior",
            "candidate_type is metadata only and is never used as an observed label",
            "Gate selection results and main-task scores are not read as labels",
        ],
        "read_scope": [
            "benchmark/gate_cases/<eight frozen NSRR candidate IDs>/",
            "benchmark/private/gate_audit/rubric.json",
            "benchmark/private/gate_audit/tasks/<eight linked independent audit IDs>/",
            "benchmark/private/gate_audit/tasks/<eight linked independent audit IDs>/fixture/ (hashed and separately qualified; not used to derive labels)",
        ],
        "excluded_sources": [
            "benchmark/private/gate_selection/",
            "benchmark/private/acceptance/",
            "benchmark/private/regression/",
            "benchmark/private/references/",
            "benchmark/scenarios/*/t4_heldout/",
            "benchmark/configs/gate_task_catalog.json",
            "main-task learning scores or Gate decisions",
        ],
        "candidate_type_counts": {
            kind: sum(item["candidate_type"] == kind for item in outputs)
            for kind in sorted(ALLOWED_TYPES)
        },
        "observed_label_counts": {
            label: sum(item["observed_label"] == label for item in outputs)
            for label in ("harmful", "neutral", "beneficial", "unknown")
        },
        "observed_labels": outputs,
    }
    labels_doc = {
        "audit_version": AUDIT_VERSION,
        "rubric_id": RUBRIC_ID,
        "freeze_manifest_sha256": freeze_sha,
        "observed_labels": outputs,
    }
    return report, labels_doc


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# NSRR Offline Independent Audit",
        "",
        f"- Audit version: `{report['audit_version']}`",
        f"- Rubric: `{report['rubric_id']}`",
        f"- Frozen input manifest SHA-256: `{report['freeze_manifest_sha256']}`",
        f"- Execution: {report['execution']}",
        "- Scope: fixture, observation, hashing, and label pipeline validation only.",
        "- Model/API calls: 0.",
        "",
        "| Candidate ID | Candidate type | Observed label | Baseline | Candidate | Rule |",
        "|---|---|---|---:|---:|---|",
    ]
    for row in report["observed_labels"]:
        baseline = f"{row['baseline_observation']['passed']}/{row['baseline_observation']['total']}"
        candidate = f"{row['candidate_observation']['passed']}/{row['candidate_observation']['total']}"
        lines.append(f"| `{row['candidate_id']}` | `{row['candidate_type']}` | **{row['observed_label']}** | {baseline} | {candidate} | `{row['rule_id']}` |")
    lines.extend([
        "",
        "## Interpretation boundary",
        "",
        "This report validates a deterministic, independent synthetic audit fixture. It does not run or approximate an LLM Gate and cannot support claims about real Gate effectiveness. Candidate categories are metadata; labels come only from paired outputs against the locked independent assertions. The cache candidate is `unknown` when performance telemetry is below the frozen minimum, even if its benefit is claimed.",
        "",
        "The deterministic label runner uses the eight candidate artifacts and the isolated `private/gate_audit` rubric, task cases, and trusted expected outputs. Fixture repos, hidden acceptance sources, task descriptions, and references are included in the freeze hash and separately qualified offline; their code is not executed to derive audit labels. The process does not consume Gate-selection outputs, main-task T4 data, main-task acceptance/regression suites, or main-task learning scores.",
        "",
    ])
    return "\n".join(lines)


def write_outputs(report: dict[str, Any], labels_doc: dict[str, Any]) -> None:
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    LABELS_PATH.write_bytes(_canonical_json(labels_doc))
    REPORT_JSON.write_bytes(_canonical_json(report))
    REPORT_MD.write_text(_render_markdown(report), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-freeze-manifest", action="store_true", help="write the initial frozen input hash manifest")
    parser.add_argument("--check", action="store_true", help="run and validate in memory without writing reports")
    args = parser.parse_args(argv)
    try:
        if args.write_freeze_manifest:
            manifest = write_freeze_manifest()
            print(f"wrote {FREEZE_PATH} with {len(manifest['inputs'])} locked inputs")
            return 0
        report, labels_doc = run_audit()
        if not args.check:
            write_outputs(report, labels_doc)
        counts = report["observed_label_counts"]
        print("NSRR offline audit passed: " + ", ".join(f"{key}={counts[key]}" for key in ("harmful", "neutral", "beneficial", "unknown")))
        print("model_api_calls=0; real_gate_effectiveness=not_tested")
        return 0
    except Exception as exc:  # fail closed; no label output on invalid input
        print(f"NSRR offline audit failed closed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
