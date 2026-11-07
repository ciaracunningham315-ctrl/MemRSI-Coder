"""Offline checks for the Raw-RAG black-box protocol and runner helpers."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_legacy = _load("live_memory_ablation", ROOT / "scripts" / "live_memory_ablation.py")
_runner = _load("rawrag_blackbox", ROOT / "scripts" / "rawrag_blackbox.py")


def test_preflight_reads_real_fixture_manifest_without_submitting_tasks(tmp_path, monkeypatch, capsys):
    output = tmp_path / "preflight.json"
    monkeypatch.setattr("sys.argv", [
        "live_memory_ablation.py", "--manifest",
        str(ROOT / "tests/fixtures/prevalidation/rawrag/manifest.json"),
        "--task-id", "wrong_scope", "--output", str(output),
    ])
    assert _legacy.main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "preflight"
    assert report["task_count"] == 1
    assert "no HTTP requests" in report["note"]


def test_manifest_fixtures_have_distinct_wrong_scope_training_identity():
    _, _, tasks = _runner.read_manifest(ROOT / "tests/fixtures/prevalidation/rawrag/manifest.json")
    same_scope = next(task for task in tasks if task["task_id"] == "convention_reuse")
    wrong_scope = next(task for task in tasks if task["task_id"] == "wrong_scope")
    assert same_scope["training_scope"] == "same_project_mapped"
    assert wrong_scope["training_scope"] == "foreign_scope_unmapped"
    assert _runner.repository_id(wrong_scope["_training_workspace"]) != _runner.repository_id(wrong_scope["_workspace"])


def test_evidence_injection_requires_context_linked_to_model_usage():
    telemetry = {"evidence_contexts": [
        {"role": "coder", "context_id": "ctx-1", "evidence_refs": ["evt-1"], "skill_refs": []},
        {"role": "planner", "context_id": None, "evidence_refs": ["evt-2"], "skill_refs": []},
        {"role": "coder", "context_id": "ctx-3", "evidence_refs": [], "skill_refs": ["skill-1"]},
    ]}
    proofs = _runner._injection_proofs(telemetry)
    assert len(proofs) == 1
    assert proofs[0]["context_id"] == "ctx-1"
    assert proofs[0]["evidence_refs"] == ["evt-1"]
    assert proofs[0]["observed_model_request"] is True


def test_rawrag_health_requires_real_hybrid_retrieval_and_keeps_failures():
    on = _runner._condition_health("ON", {"status": "failed", "telemetry": {
        "retrievals": [{"query": "task context", "mode": "fts", "status": "degraded"}],
        "model_calls": 1,
    }}, "provider unavailable")
    assert on["rawrag_condition_clear"] is False
    assert on["branch_returned"] is True
    assert on["branch_error"] == "provider unavailable"
    assert on["retrievals"][0]["query"] == "task context"

    off = _runner._condition_health("OFF", {"status": "completed", "telemetry": {
        "retrievals": [{"query": "task context", "mode": "hybrid", "status": "ok"}],
    }}, None)
    assert off["rawrag_condition_clear"] is False


def test_task_acceptance_failure_is_separate_from_agent_completion():
    outcome = _runner._outcome("completed", {
        "acceptance_protected": True, "passed": False, "acceptance_output": "check failed",
    })
    assert outcome["agent_execution_completed"] is True
    assert outcome["task_passed"] is False
    assert outcome["acceptance_outcome"] == "failed"
    assert outcome["failure_reason"] == "check failed"
    assert outcome["scored_outcome"] == "fail"
    assert outcome["outcome_scoreable"] is True
    assert outcome["evaluation_infrastructure_failure"] is False


def test_noncompletion_is_scored_and_infrastructure_oracle_gap_is_not():
    failed_agent = _runner._outcome("failed", {
        "acceptance_protected": True, "passed": False,
    })
    missing_oracle = _runner._outcome("completed", None)
    missing_status = _runner._outcome(None, None)
    assert failed_agent["scored_outcome"] == "noncompletion"
    assert failed_agent["outcome_scoreable"] is True
    assert missing_oracle["scored_outcome"] == "unscored_oracle_unavailable"
    assert missing_oracle["evaluation_infrastructure_failure"] is True
    assert missing_status["scored_outcome"] == "unscored_infrastructure"


def test_no_retrieval_is_readiness_failure_but_pair_remains_oracle_scored():
    on = _runner._condition_health("ON", {
        "status": "completed", "telemetry": {"retrievals": [], "tokens_complete": True},
    }, None)
    off = _runner._condition_health("OFF", {
        "status": "completed", "telemetry": {"retrievals": [], "tokens_complete": True},
    }, None)
    on.update(_runner._outcome("completed", {"acceptance_protected": True, "passed": True}))
    off.update(_runner._outcome("completed", {"acceptance_protected": True, "passed": False}))
    on.update({"acceptance_aligned": True, "planned_acceptance_sha256": "oracle-sha"})
    off.update({"acceptance_aligned": True, "planned_acceptance_sha256": "oracle-sha"})
    assert on["readiness_diagnostics"]["hybrid_retrieval_ready"] is False
    assert on["readiness_diagnostics"]["evidence_exposure_ready"] is False
    assert _runner._pair_scoreable(on, off, aligned=True)
    assert on["scored_outcome"] == "pass"
    assert off["scored_outcome"] == "fail"


def test_known_noncompletion_needs_only_common_planned_oracle_hash_for_pair_eligibility():
    on = {"scored_outcome": "noncompletion", "outcome_scoreable": True,
          "acceptance_aligned": False, "planned_acceptance_sha256": "oracle-sha"}
    off = {"scored_outcome": "pass", "outcome_scoreable": True,
           "acceptance_aligned": True, "planned_acceptance_sha256": "oracle-sha"}
    assert _runner._pair_scoreable(on, off, aligned=True)
    off["planned_acceptance_sha256"] = "different-oracle"
    assert not _runner._pair_scoreable(on, off, aligned=True)


def test_score_summary_counts_outcomes_and_only_eligible_paired_wins():
    report = _runner._score_summary([{
        "eligible_for_comparison": True,
        "records": [
            {"condition": "ON", "scored_outcome": "pass", "outcome_scoreable": True},
            {"condition": "OFF", "scored_outcome": "noncompletion", "outcome_scoreable": True},
        ],
    }, {
        "eligible_for_comparison": False,
        "records": [
            {"condition": "ON", "scored_outcome": "unscored_infrastructure", "outcome_scoreable": False},
            {"condition": "OFF", "scored_outcome": "fail", "outcome_scoreable": True},
        ],
    }])
    assert report["conditions"]["ON"] == {
        "planned": 2, "scored": 1, "pass": 1, "fail": 0,
        "noncompletion": 0, "unscored": 1,
    }
    assert report["conditions"]["OFF"]["noncompletion"] == 1
    assert report["paired_wins"]["eligible_pairs"] == 1
    assert report["paired_wins"]["ON_wins"] == 1


@pytest.mark.asyncio
async def test_off_warmup_reads_memory_job_status_once_without_waiting():
    class StubInstance:
        timeout = 600

        def __init__(self):
            self.calls = 0

        async def request(self, method, path):
            self.calls += 1
            assert (method, path) == ("GET", "/api/memory")
            return {"jobs": {"pending": 10, "processing": 1}}

    instance = StubInstance()
    status = await _runner._memory_index_snapshot(instance, "OFF")
    assert status["jobs"]["pending"] == 10
    assert instance.calls == 1


def test_provider_or_execution_exception_is_infrastructure_not_candidate_failure():
    outcome = _runner._outcome("not_returned", None, "provider unavailable")
    assert outcome["scored_outcome"] == "unscored_infrastructure"
    assert outcome["outcome_scoreable"] is False
    assert outcome["evaluation_infrastructure_failure"] is True


def test_setup_abort_keeps_every_planned_pair_without_rerunning(monkeypatch, capsys):
    def fail_before_execute(coroutine):
        coroutine.close()
        raise RuntimeError("Qdrant unavailable before held-out submission")

    monkeypatch.setattr(_runner.asyncio, "run", fail_before_execute)
    assert _runner.main(["--execute"]) == 2
    report = json.loads(capsys.readouterr().out)
    planned = json.loads((ROOT / "tests/fixtures/prevalidation/rawrag/manifest.json").read_text())[
        "task_count"]
    assert report["planned_pairs"] == planned
    assert report["scored_pairs"] == 0
    assert len(report["records"]) == planned
    assert report["paired_wins"]["eligible_pairs"] == 0
    assert all(len(pair["records"]) == 2 for pair in report["records"])
    assert "Qdrant unavailable" in report["execution_error"]


def test_offline_legacy_assertion_rejects_memory_context():
    request = [{"messages": [{"role": "user", "content": "Untrusted retrieved evidence: secret"}],
                "tools": [], "evidence": []}]
    with pytest.raises(ValueError, match="injected Raw-RAG context"):
        _legacy.assert_off_isolated(request)
