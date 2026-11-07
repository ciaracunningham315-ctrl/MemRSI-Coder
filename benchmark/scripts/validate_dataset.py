"""Run offline dataset admission without invoking any model service."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.dataset_validation import (
    applicability_probe,
    render_markdown,
    validate_dataset,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--repository-id", choices=("repo_api", "repo_cli", "repo_data"), default="repo_api")
    parser.add_argument("--probe-python", help="Python environment containing sibling Agent dependencies")
    parser.add_argument("--internal-probe", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--probe-work", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.internal_probe:
        agent = root.parent
        sys.path.insert(0, str(agent / "src"))
        report = applicability_probe(root, args.probe_work)
        args.internal_probe.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        report = validate_dataset(root, args.python, args.probe_python, args.repository_id)
        reports = root / "reports"
        reports.mkdir(exist_ok=True)
        prefix = args.repository_id
        (reports / f"{prefix}_dataset_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (reports / f"{prefix}_dataset_validation.md").write_text(render_markdown(report), encoding="utf-8")
        print(f"Offline dataset admission: {'PASS' if report['passed'] else 'FAIL'}")
        print(reports / f"{prefix}_dataset_validation.json")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
