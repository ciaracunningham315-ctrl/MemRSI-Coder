#!/usr/bin/env python3
"""Qualify the eight independent NSRR sandbox fixtures without model calls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runner.nsrr_audit_tasks import qualify_nsrr_fixtures  # noqa: E402


def render_markdown(report: dict) -> str:
    lines = [
        "# NSRR Sandbox Fixture Qualification",
        "",
        "This is offline engineering evidence for independent synthetic fixtures only.",
        "It does not evaluate an Agent, candidate Skill, real Gate, main T4 task, or main-task score.",
        "",
        f"- Tasks: {report['task_count']}",
        f"- Sandbox-compatible TaskSpecs materialized: {report['sandbox_task_specs_materialized']}/{report['task_count']}",
        "- Model/API/embedding/reranker calls: 0",
        "- Formal results: no",
        "- Main-task score eligible: no",
        "",
        "| Audit task | TaskSpec | Baseline public | Baseline private target | Reference public | Reference private target | Reference combined | Fingerprint |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in report["tasks"]:
        checks = row["checks"]
        status = lambda key: "PASS" if checks[key] else "FAIL"
        lines.append(
            f"| `{row['task_id']}` | {status('sandbox_task_spec_validates')} | {status('baseline_public_passes')} | {status('baseline_target_fails')} | "
            f"{status('reference_public_passes')} | {status('reference_target_passes')} | "
            f"{status('reference_combined_passes')} | `{row['fixed_input_fingerprint'][:12]}…` |"
        )
    lines.extend([
        "",
        f"Overall qualification: **{'PASS' if report['passed'] else 'FAIL'}**.",
        "",
        "Each prompt contains the business goal and public function surface. Private cases and expected outputs are mounted only as host-side protected acceptance inputs. Baseline stubs are expected to pass the import-only public check and fail the private target assertions; reference implementations must pass the target and combined suites.",
        "",
        "This qualification runs local Python unit tests and validates each materialized host-side spec against the current `evocode.contracts.TaskSpec` model. It does not invoke SandboxEvaluator, an Agent, a model, an API, an embedding service, or a reranker. The public TaskSpec fields are `repo_path`, `prompt`, `acceptance_command`, and `repository_id`; SandboxEvaluator consumes `commit` and host-only `protected_acceptance_paths` from the surrounding spec dictionary.",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="offline fixture Python interpreter")
    args = parser.parse_args(argv)
    report = qualify_nsrr_fixtures(ROOT, python=args.python)
    report_path = ROOT / "reports" / "nsrr_audit_fixture_qualification.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path = report_path.with_suffix(".md")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print("NSRR sandbox fixture qualification:", "PASS" if report["passed"] else "FAIL")
    print(f"tasks={report['task_count']}; task_specs={report['sandbox_task_specs_materialized']}; model_calls=0; api_calls=0; main_task_score_eligible=false")
    print(markdown_path)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
