import json
from contextlib import nullcontext

import pytest

from scripts.live_evolution_baseline import (
    compact_runner,
    import_task_history,
    meta_evaluation_gates,
    validate_prior_task_comparison,
)


def _valid_source_report():
    result = {"passed": True, "acceptance_protected": True}
    return {
        "status": "completed",
        "task_skill_proposal": {"candidate_name": "prior-skill"},
        "task_skill_comparison": {
            "status": "evaluated",
            "passed": True,
            "rows": [{"baseline": result.copy(), "candidate": result.copy()}],
        },
    }


def test_prior_task_history_rejects_failed_candidate_before_persisting(tmp_path):
    source = _valid_source_report()
    source["task_skill_comparison"]["rows"][0]["candidate"]["passed"] = False
    path = tmp_path / "prior.json"
    path.write_text(json.dumps(source), encoding="utf-8")

    class Store:
        def transaction(self):
            raise AssertionError("invalid evidence must be rejected before database writes")

    with pytest.raises(ValueError, match="candidate task acceptance failed"):
        import_task_history(type("App", (), {"store": Store()})(), "task-id", path)


def test_import_accepts_successful_task_pair_from_report_with_downstream_failure(tmp_path):
    source = _valid_source_report()
    source.update(status="failed", error_type="ReuseFailure", error="reuse_validation_failed")
    path = tmp_path / "prior.json"
    path.write_text(json.dumps(source), encoding="utf-8")

    class Connection:
        def __init__(self):
            self.params = None

        def execute(self, _sql, params):
            self.params = params

    connection = Connection()
    app = type("App", (), {"store": type("Store", (), {
        "transaction": lambda self: nullcontext(connection),
    })()})()

    imported = import_task_history(app, "task-id", path)

    assert imported["source_boundary"] == {
        "report_status": "failed",
        "report_error_type": "ReuseFailure",
        "report_error": "reuse_validation_failed",
        "task_skill_comparison_status": "evaluated",
        "task_skill_comparison_passed": True,
    }
    persisted_payload = json.loads(connection.params[5])
    assert persisted_payload["source_run_boundary"] == imported["source_boundary"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda source: source["task_skill_comparison"].update(status="failed"),
            "comparison was not evaluated",
        ),
        (lambda source: source["task_skill_comparison"].update(passed=False), "did not pass"),
        (
            lambda source: source["task_skill_comparison"]["rows"][0]["baseline"].update(
                acceptance_protected=False
            ),
            "baseline acceptance tests were not protected",
        ),
    ],
)
def test_prior_task_history_requires_completed_successful_protected_pair(mutate, message):
    source = _valid_source_report()
    mutate(source)

    with pytest.raises(ValueError, match=message):
        validate_prior_task_comparison(source, source["task_skill_comparison"])


def test_compact_runner_names_protection_and_task_result_separately():
    compact = compact_runner({"passed": False, "acceptance_protected": True, "error": "AssertionError"})

    assert compact["tests_protected"] is True
    assert compact["task_passed"] is False
    assert compact["failure_reason"] == "AssertionError"


def _meta_comparison():
    return {
        "rows": [{
            "baseline": {
                "tests_protected": True,
                "task_passed": True,
                "coder_skill_hashes": ["base-hash"],
            },
            "candidate": {
                "tests_protected": True,
                "task_passed": True,
                "coder_skill_hashes": ["candidate-hash"],
            },
        }],
        "meta_stages": [
            {"branch": "baseline", "content_sha256": "base-hash"},
            {"branch": "candidate", "content_sha256": "candidate-hash"},
        ],
    }


def test_meta_evaluation_requires_both_generated_skill_hashes_in_coder():
    comparison = _meta_comparison()
    comparison["rows"][0]["candidate"]["coder_skill_hashes"] = []

    gates = meta_evaluation_gates(comparison)

    assert gates["both_generated_skills_reached_coder"] is False
    assert gates["both_branches_verified"] is True
    assert gates["passed"] is False
    assert gates["failure_reasons"] == ["candidate_generated_skill_not_injected_row_0"]


def test_protected_but_failed_task_is_not_verified():
    comparison = _meta_comparison()
    comparison["rows"][0]["candidate"].update(task_passed=False, failure_reason="AssertionError")

    gates = meta_evaluation_gates(comparison)

    assert gates["both_branches_verified"] is False
    assert gates["passed"] is False
    assert gates["failure_reasons"] == ["candidate_task_failed_row_0: AssertionError"]
