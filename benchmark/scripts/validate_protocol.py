"""Validate the frozen benchmark configuration without running models.

The readiness check covers only declared IDs, not their backing task files,
scope, privacy or stage provenance. The runner must verify those separately.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "configs" / "protocol.yaml"


def validate(config: dict, ready_profile: str | None = None) -> list[str]:
    errors: list[str] = []

    def require(condition, message):
        if not condition:
            errors.append(message)

    profiles = config.get("profiles", {})
    expected_repos = {
        "chain_integration": ["repo_api"],
        "formal_benchmark": ["repo_api", "repo_cli", "repo_data"],
    }
    require(set(profiles) == set(expected_repos), "profiles must be chain_integration and formal_benchmark")
    execution = config.get("execution", {})
    require(execution.get("background_indexing") is False, "background indexing must be disabled")
    require(execution.get("auto_trigger_enabled") is False, "automatic evolution must be disabled")
    require(execution.get("stage_indexing") == "explicit_incremental", "stage indexing must be incremental")
    require(type(execution.get("max_total_tokens")) is int and execution["max_total_tokens"] > 0, "execution.max_total_tokens must be a positive integer")
    b = config.get("experiment_b", {})
    require(all(b.get("conditions", {}).get(group, {}).get("raw_rag") is True for group in ("B2", "B3", "B4")), "all B groups require Raw-RAG")
    require(b.get("heldout_learning") is False, "held-out learning must be disabled")
    for name, repos in expected_repos.items():
        profile = profiles.get(name, {})
        require(profile.get("repositories") == repos, f"{name}: invalid repositories")
        require(profile.get("meta_review_repositories") == repos, f"{name}: invalid Meta barrier")
        repeats = 1
        per_repeat = len(repos) * (3 * 2 + 4 * 3)
        require(profile.get("repeats") == repeats, f"{name}: invalid repeat count")
        require(profile.get("seeds") == [42], f"{name}: seeds must be [42]")
        require(profile.get("main_runs_per_repeat") == per_repeat and profile.get("total_main_runs") == repeats * per_repeat, f"{name}: incorrect run counts")
        require(profile.get("formal_results") is (name == "formal_benchmark"), f"{name}: invalid results flag")
        gates = profile.get("gate_task_ids", {})
        regression = profile.get("regression_task_ids", {})
        require(isinstance(gates, dict) and set(gates) == {"seed", "transfer", "stale"}, f"{name}: stage gates missing")
        require(isinstance(regression, dict) and set(regression) == set(repos), f"{name}: regression scopes invalid")
        nsrr = profile.get("nsrr", {})
        formal = name == "formal_benchmark"
        require(nsrr.get("enabled") is formal and nsrr.get("candidate_count") == (8 if formal else 0), f"{name}: NSRR setting invalid")
        if ready_profile == name:
            for repo in repos:
                ids = regression.get(repo)
                require(isinstance(ids, list) and bool(ids) and all(isinstance(x, str) and x for x in ids), f"{name}: regression IDs unresolved for {repo}")
            for stage in ("seed", "transfer", "stale"):
                matrix = gates.get(stage)
                require(isinstance(matrix, dict), f"{name}: gate IDs unresolved for {stage}")
                if isinstance(matrix, dict):
                    general = matrix.get("general")
                    require(isinstance(general, list) and all(isinstance(x, str) and x for x in general), f"{name}: General gate IDs undeclared for {stage}")
                    project = matrix.get("project", {})
                    require(isinstance(project, dict) and set(project) == set(repos), f"{name}: project gate scopes invalid for {stage}")
                    for repo in repos:
                        ids = project.get(repo) if isinstance(project, dict) else None
                        require(isinstance(ids, list) and bool(ids) and all(isinstance(x, str) and x for x in ids), f"{name}: project gate IDs unresolved for {stage}/{repo}")
                    if stage == "transfer":
                        ids = matrix.get("meta")
                        require(isinstance(ids, list) and bool(ids) and all(isinstance(x, str) and x for x in ids), f"{name}: Meta gate IDs unresolved")
            if formal:
                ids = nsrr.get("audit_task_ids")
                require(isinstance(ids, list) and bool(ids) and all(isinstance(x, str) and x for x in ids), f"{name}: NSRR audit IDs unresolved")
    if ready_profile is not None and ready_profile not in expected_repos:
        errors.append(f"unknown profile: {ready_profile}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-ready", choices=("chain_integration", "formal_benchmark"))
    args = parser.parse_args()
    try:
        config = yaml.safe_load(PROTOCOL.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("configuration must be a mapping")
        errors = validate(config, args.require_ready)
    except (OSError, yaml.YAMLError, ValueError, TypeError, AttributeError) as exc:
        print(f"Invalid protocol: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("Protocol configuration valid" + (f" for {args.require_ready} declared IDs" if args.require_ready else "; task IDs may remain unresolved"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
