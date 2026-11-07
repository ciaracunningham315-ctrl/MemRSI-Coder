#!/usr/bin/env python3
"""Offline admission for repo_data publication and regression gates; no model calls."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = [
    "private/gate_selection/repo_data/repo_data_gate_seed_receipt/manifest.json",
    "private/gate_selection/repo_data/repo_data_gate_transfer_reading/manifest.json",
    "private/gate_selection/repo_data/repo_data_gate_stale_telemetry/manifest.json",
    "private/gate_selection/repo_data/repo_data_gate_meta_source_merge/manifest.json",
    "private/regression_replay/repo_data/repo_data_replay_timecard_rounding/manifest.json",
]
PATH_KEYS = ("code_dir", "private_tests_dir", "regression_tests_dir", "reference_dir")
ALLOWED_STAGES = {"seed", "transfer", "stale"}


def relpath(value: str) -> Path:
    path = (ROOT / value).resolve(strict=True)
    if not path.is_relative_to(ROOT):
        raise ValueError(f"unsafe path: {value}")
    return path


def inventory(directory: Path) -> dict[str, str]:
    ignored = {".DS_Store", "__pycache__", ".pytest_cache"}
    values = {}
    for path in sorted(directory.rglob("*")):
        if any(part in ignored for part in path.relative_to(directory).parts):
            continue
        if path.is_symlink():
            raise ValueError(f"symlink is forbidden: {path}")
        if path.is_file():
            values[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return values


def fingerprints(manifest: dict) -> dict:
    result = {key: inventory(relpath(manifest[key])) for key in PATH_KEYS}
    result["task_file"] = hashlib.sha256(relpath(manifest["task_file"]).read_bytes()).hexdigest()
    return result


def run(argv: list[str], cwd: Path, log: Path) -> dict:
    log.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    proc = subprocess.run(argv, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=60)
    log.write_text(proc.stdout, encoding="utf-8")
    counts = re.findall(r"Ran (\d+) tests? in", proc.stdout)
    failures = re.findall(r"failures=(\d+)", proc.stdout)
    errors = re.findall(r"errors=(\d+)", proc.stdout)
    return {"argv": argv, "exit_code": proc.returncode, "tests_run": int(counts[-1]) if counts else None,
            "test_failures": int(failures[-1]) if failures else 0,
            "test_errors": int(errors[-1]) if errors else 0,
            "log": str(log.relative_to(ROOT))}


def passed(item: dict) -> bool:
    return item["exit_code"] == 0 and bool(item["tests_run"])


def suite(root: Path, manifest: dict, variant: str, name: str, include: tuple[str, ...], log_root: Path) -> dict:
    scratch = root / (name + "_" + uuid.uuid4().hex)
    source = relpath(manifest["code_dir"] if variant == "baseline" else manifest["reference_dir"])
    shutil.copytree(source, scratch)
    tests_dir = scratch / "tests"
    tests_dir.mkdir(exist_ok=True)
    for key in include:
        for test_file in sorted(relpath(manifest[key]).rglob("*.py")):
            destination = tests_dir / test_file.name
            if destination.exists():
                shutil.rmtree(scratch)
                raise ValueError(f"test collision: {destination.name}")
            shutil.copyfile(test_file, destination)
    log = log_root / f"{variant}_{name}.log"
    argv = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    result = run(argv, scratch, log)
    shutil.rmtree(scratch)
    return result


def main() -> int:
    records = []
    for manifest_rel in MANIFESTS:
        manifest_path = relpath(manifest_rel)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        task_id = manifest.get("task_id")
        if manifest.get("repository_id") != "repo_data" or manifest.get("task_id") not in manifest_rel:
            raise ValueError(f"manifest identity mismatch: {manifest_rel}")
        if manifest.get("stage") not in ALLOWED_STAGES:
            raise ValueError(f"unsupported gate stage: {manifest_rel}")
        if manifest.get("kind") not in ("task", "meta", "regression"):
            raise ValueError(f"unsupported gate kind: {manifest_rel}")
        if manifest.get("independent_of_main_tasks") is not True or manifest.get("future_main_inputs_used") != []:
            raise ValueError(f"non-independent gate provenance: {task_id}")
        if manifest.get("acceptance_command") != ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]:
            raise ValueError(f"unexpected command: {task_id}")
        expected_fingerprints = fingerprints(manifest)
        if expected_fingerprints != manifest.get("fingerprints"):
            raise ValueError(f"frozen fingerprint mismatch: {task_id}")
        task_root = manifest_path.parent
        qualification = task_root / "qualification"
        qualification.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="gate-check-", dir=qualification) as temp:
            work = Path(temp)
            logs = qualification
            baseline = {
                "public": suite(work, manifest, "baseline", "public", (), logs),
                "target": suite(work, manifest, "baseline", "target", ("private_tests_dir",), logs),
                "regression": suite(work, manifest, "baseline", "regression", ("regression_tests_dir",), logs),
                "combined": suite(work, manifest, "baseline", "combined",
                                  ("private_tests_dir", "regression_tests_dir"), logs),
            }
            reference = {
                "public": suite(work, manifest, "reference", "public", (), logs),
                "target": suite(work, manifest, "reference", "target", ("private_tests_dir",), logs),
                "regression": suite(work, manifest, "reference", "regression", ("regression_tests_dir",), logs),
                "combined": suite(work, manifest, "reference", "combined",
                                  ("private_tests_dir", "regression_tests_dir"), logs),
            }
        checks = {
            "baseline_public_passes": passed(baseline["public"]),
            "baseline_target_fails_without_test_errors": baseline["target"]["exit_code"] != 0
                and baseline["target"]["test_failures"] > 0 and baseline["target"]["test_errors"] == 0,
            "baseline_regression_passes": passed(baseline["regression"]),
            "reference_all_pass": all(passed(item) for item in reference.values()),
        }
        evidence = {
            "task_id": task_id, "repository_id": "repo_data", "stage": manifest["stage"],
            "kind": manifest["kind"], "checks": checks, "passed": all(checks.values()),
            "future_main_inputs_used": [], "independent_of_main_tasks": True,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "fingerprints": expected_fingerprints, "baseline": baseline, "reference": reference,
            "model_calls": 0, "formal_results": False,
            "boundary": "Offline fixture qualification only; no model-generated Agent result or publication benefit is measured."
        }
        (qualification / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        records.append(evidence)
    report = {
        "kind": "repo_data_independent_gate_and_replay_qualification",
        "passed": all(item["passed"] for item in records), "model_calls": 0, "formal_results": False,
        "tasks": records,
        "boundary": "No model calls; validates independent fixture structure and baseline/reference execution only."
    }
    out = ROOT / "reports/repo_data_gate_validation.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# repo_data 独立 Gate / regression replay 离线验收", "",
             "模型调用为 0。此结果仅验证独立夹具与判分可运行性，不代表模型、技能收益或正式基准结果。", "",
             "| Task ID | Stage | Kind | 基线公开 | 基线目标失败 | 基线回归 | 参考全通过 |",
             "|---|---|---|---|---|---|---|"]
    for item in records:
        c = item["checks"]
        lines.append(f"| {item['task_id']} | {item['stage']} | {item['kind']} | {c['baseline_public_passes']} | {c['baseline_target_fails_without_test_errors']} | {c['baseline_regression_passes']} | {c['reference_all_pass']} |")
    (ROOT / "reports/repo_data_gate_validation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"repo_data independent Gate/replay admission: {'PASS' if report['passed'] else 'FAIL'}")
    print(out)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
