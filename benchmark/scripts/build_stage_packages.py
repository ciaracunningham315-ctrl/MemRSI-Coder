#!/usr/bin/env python3
"""Build reproducible stage-local Git packages (offline; no model calls)."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.stage_packages import build_stage_packages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--repository-id", default="repo_api")
    args = parser.parse_args()
    manifests = build_stage_packages(args.root, args.repository_id)
    print(json.dumps({"repository_id": args.repository_id, "packages": [
        {key: item[key] for key in ("task_id", "git_stage_package", "git_stage_package_sha256",
                                    "code_snapshot_sha256", "visible_head_sha", "allowed_ancestor_shas")}
        for item in manifests]}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
