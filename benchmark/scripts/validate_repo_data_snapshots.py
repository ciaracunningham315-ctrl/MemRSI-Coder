#!/usr/bin/env python3
"""Verify repo_data fixed Git snapshots and stage-local object visibility offline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.stage_packages import code_snapshot_sha256, validate_stage_package

STAGES = ("t1_seed", "t2_transfer", "t3_stale", "t4_heldout")


def main() -> int:
    records = []
    with tempfile.TemporaryDirectory(prefix="repo-data-package-check-") as td:
        temp_root = Path(td)
        (temp_root / "scenarios").mkdir()
        shutil.copytree(ROOT / "scenarios/repo_data", temp_root / "scenarios/repo_data")
        (temp_root / ".runtime").mkdir()
        for stage in STAGES:
            manifest_path = temp_root / f"scenarios/repo_data/{stage}/manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            package = temp_root / manifest["git_stage_package"]
            package.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / manifest["git_stage_package"], package)
            package_sha = hashlib.sha256(package.read_bytes()).hexdigest()
            source_sha = code_snapshot_sha256(temp_root / manifest["code_dir"])
            verified = validate_stage_package(temp_root, manifest)
            expected_ancestors = STAGES.index(stage)
            record = {
                "task_id": manifest["task_id"], "stage_dir": stage,
                "visible_head_sha": manifest["visible_head_sha"],
                "allowed_ancestor_shas": manifest["allowed_ancestor_shas"],
                "git_object_shas_count": len(manifest["git_object_shas"]),
                "expected_ancestor_count": expected_ancestors,
                "package_sha256": package_sha,
                "manifest_package_sha256": manifest["git_stage_package_sha256"],
                "source_code_snapshot_sha256": source_sha,
                "manifest_code_snapshot_sha256": manifest["code_snapshot_sha256"],
                "verified_package": verified,
                "passed": package_sha == manifest["git_stage_package_sha256"]
                    and source_sha == manifest["code_snapshot_sha256"]
                    and verified["visible_head_sha"] == manifest["visible_head_sha"]
                    and verified["allowed_ancestor_shas"] == manifest["allowed_ancestor_shas"]
                    and len(manifest["allowed_ancestor_shas"]) == expected_ancestors,
            }
            records.append(record)
    report = {
        "kind": "repo_data_fixed_stage_package_verification",
        "passed": all(row["passed"] for row in records),
        "model_calls": 0, "formal_results": False, "stages": records,
        "boundary": "Verifies frozen source hashes and stage-local Git package ancestry/object reachability. It is not a model trajectory or Agent ability result."
    }
    out = ROOT / "private/references/repo_data/qualification/stage_package_validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"repo_data stage-package verification: {'PASS' if report['passed'] else 'FAIL'}")
    print(out)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
