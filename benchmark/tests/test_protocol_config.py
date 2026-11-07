from copy import deepcopy
import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("benchmark_validate_protocol", ROOT / "scripts" / "validate_protocol.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def config():
    return yaml.safe_load((ROOT / "configs" / "protocol.yaml").read_text(encoding="utf-8"))


def test_chain_and_formal_declared_ids_are_ready():
    value = config()
    assert module.validate(value) == []
    assert module.validate(value, "chain_integration") == []
    assert module.validate(value, "formal_benchmark") == []


def test_protocol_detects_cross_scope_and_wrong_count():
    value = deepcopy(config())
    value["profiles"]["chain_integration"]["meta_review_repositories"].append("repo_cli")
    value["profiles"]["formal_benchmark"]["total_main_runs"] = 162
    errors = module.validate(value)
    assert any("Meta barrier" in error for error in errors)
    assert any("incorrect run counts" in error for error in errors)


def test_formal_protocol_is_single_repeat_with_seed_42_and_54_runs():
    value = config()
    formal = value["profiles"]["formal_benchmark"]
    chain = value["profiles"]["chain_integration"]

    assert value["dataset"]["main_task_count"] == 12
    assert value["execution"]["max_total_tokens"] == 100_000_000
    assert formal["repeats"] == 1
    assert formal["seeds"] == [42]
    assert formal["main_runs_per_repeat"] == formal["total_main_runs"] == 54
    assert chain["repeats"] == 1
    assert chain["seeds"] == [42]
    assert chain["main_runs_per_repeat"] == chain["total_main_runs"] == 18


def test_protocol_rejects_extra_formal_repeat_or_wrong_seed():
    value = deepcopy(config())
    value["profiles"]["formal_benchmark"]["repeats"] = 3
    value["profiles"]["formal_benchmark"]["seeds"] = [42, 43, 44]

    errors = module.validate(value)
    assert any("invalid repeat count" in error for error in errors)
    assert any("seeds must be [42]" in error for error in errors)


def test_protocol_rejects_nonpositive_default_token_budget():
    value = deepcopy(config())
    value["execution"]["max_total_tokens"] = 0

    assert any("max_total_tokens must be a positive integer" in error for error in module.validate(value))


def test_ready_check_rejects_missing_meta_and_other_repository_scope():
    value = config()
    profile = value["profiles"]["chain_integration"]
    profile["regression_task_ids"] = {"repo_api": ["regression-case"]}
    profile["gate_task_ids"] = {
        stage: {"project": {"repo_api": ["gate-case"]}, "general": [], "meta": ["meta-case"]}
        for stage in ("seed", "transfer", "stale")
    }
    assert module.validate(value, "chain_integration") == []
    profile["gate_task_ids"]["transfer"]["project"]["repo_cli"] = ["unrelated"]
    profile["gate_task_ids"]["transfer"]["meta"] = []
    errors = module.validate(value, "chain_integration")
    assert any("project gate scopes invalid" in error for error in errors)
    assert any("Meta gate IDs unresolved" in error for error in errors)
