"""Offline checks for the held-out EvoCode RSI fixture boundary."""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from scripts import live_rsi_prevalidation
from scripts.live_rsi_prevalidation import (
    code_fingerprint, compact, configuration_fingerprint, fixture_fingerprint, lifecycle_tokens,
    load_dataset, loaded_task_skill_links, loaded_task_skill_links_by_phase, preflight,
    proposal_reliability, select_family,
    summarize_run, task_skill_links,
)


def test_live_cli_imports_runtime_before_rejecting_unknown_family(tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(live_rsi_prevalidation.__file__).resolve()),
         "--phase", "live", "--family", "missing-fixture"],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert "Unknown family" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_prevalidation_separates_learning_selection_and_heldout_sets():
    manifest, materials, held_out = load_dataset()
    learning = {item for family in manifest["families"].values() for item in family["learning"]}
    phase_2 = {item for family in manifest["families"].values() for item in family["phase_2"]}
    selection = {item for family in manifest["families"].values() for item in family["selection"]}
    held_ids = {item for family in manifest["families"].values() for item in family["held_out"]}

    assert learning.isdisjoint(selection)
    assert learning.isdisjoint(phase_2)
    assert phase_2.isdisjoint(selection)
    assert learning.isdisjoint(held_ids)
    assert selection.isdisjoint(held_ids)
    assert phase_2.isdisjoint(held_ids)
    assert len(learning) == len(phase_2) == 4
    assert len(selection) == 2
    assert len(held_ids) == len(held_out) == 4
    assert all("live_task" in materials[item] for item in learning | phase_2 | selection)


def test_phase_2_input_validation_prompt_covers_its_oracle_semantics():
    _, materials, _ = load_dataset()
    task = materials["phase2-input-validation-regression"]["live_task"]
    prompt = task["prompt"]
    acceptance = task["acceptance"]

    # Keep the task definition explicit about every behavior enforced by its oracle.
    for semantic in (
        "exact Python int",
        "inclusive range [0, 1000]",
        "returns the same value unchanged",
        "Raise TypeError",
        "raise ValueError",
    ):
        assert semantic in prompt
    for oracle_case in (
        "for value in (0, 37, 1000):",
        "assert offset(value) == value",
        'for bad_type in (True, False, 1.5, "1"):',
        "except TypeError:",
        "for out_of_range in (-1, 1001):",
        "except ValueError:",
    ):
        assert oracle_case in acceptance


def test_offline_acceptance_fails_baseline_and_passes_reference_for_every_task():
    manifest, _, _ = load_dataset()
    report = preflight()

    assert report["status"] == "preflight_passed"
    assert report["execution_mode"] == "deterministic_fixture_preflight_only"
    assert report["model_calls"] == 0
    assert report["split_counts"] == {
        "families": 2, "learning": 4, "phase_2": 4, "selection": 2, "held_out": 4,
    }
    assert len(report["rows"]) == 4
    assert all(row["baseline"]["passed"] is False for row in report["rows"])
    assert all(row["reference"]["passed"] is True for row in report["rows"])
    assert all(row["protected_acceptance_sha256"] for row in report["rows"])
    assert len(report["phase_2_oracle_checks"]) == 4
    assert all(row["baseline"]["passed"] is False for row in report["phase_2_oracle_checks"])
    assert all(row["reference"]["passed"] is True for row in report["phase_2_oracle_checks"])
    trigger_diagnostic = report["phase_2_learning_trigger_diagnostic"]
    assert set(trigger_diagnostic["families"]) == set(manifest["families"])
    for family, coverage in trigger_diagnostic["families"].items():
        planned = len(manifest["families"][family]["phase_2"])
        assert coverage["task_count"] == planned
        assert coverage["batch_task_count"] > 0
        assert coverage["similar_issue_threshold"] > 0
        assert coverage["enough_tasks_for_repository_batch"] is True
        assert coverage["enough_tasks_for_repository_batch"] == (
            planned >= coverage["batch_task_count"])
        assert coverage["enough_tasks_for_repeated_issue"] == (
            planned >= coverage["similar_issue_threshold"])
        assert coverage["message"]


def test_family_filter_limits_all_splits_and_rejects_unknown_key():
    manifest, _, _ = load_dataset()
    subset = select_family(manifest, "api_compatibility")

    assert list(subset["families"]) == ["api_compatibility"]
    report = preflight("api_compatibility")
    assert report["families"] == ["api_compatibility"]
    assert report["split_counts"] == {
        "families": 1, "learning": 2, "phase_2": 2, "selection": 1, "held_out": 2,
    }
    try:
        select_family(manifest, "missing")
    except ValueError as exc:
        assert "Unknown family" in str(exc)
    else:
        raise AssertionError("unknown family should fail before live execution")


def test_fixture_fingerprint_covers_all_files_and_is_path_stable(tmp_path):
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    (fixture_dir / "dataset.json").write_text('{"dataset_id":"fixture-v1"}', encoding="utf-8")
    (fixture_dir / "unused.json").write_text('{"unused":true}', encoding="utf-8")

    first = fixture_fingerprint(fixture_dir, dataset_id="fixture-v1")
    assert first["dataset_id"] == "fixture-v1"
    assert [item["path"] for item in first["files"]] == ["dataset.json", "unused.json"]
    expected = "".join(f"{item['path']}\0{item['sha256']}\n" for item in first["files"])
    assert first["digest"] == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert fixture_fingerprint(fixture_dir, dataset_id="fixture-v1") == first

    (fixture_dir / "unused.json").write_text('{"unused":false}', encoding="utf-8")
    changed = fixture_fingerprint(fixture_dir, dataset_id="fixture-v1")
    assert changed["digest"] != first["digest"]


def test_code_and_configuration_fingerprints_are_deterministic():
    code = code_fingerprint()
    assert code["git_head"]
    assert code["python_sources"]
    assert code["source_digest"] == hashlib.sha256(
        json.dumps(code["python_sources"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert len(code["uncommitted_diff_sha256"]) == 64

    config = {"local_model_id": "offline-test", "max_total_tokens": 1234}
    fingerprint = configuration_fingerprint(config)
    assert fingerprint == configuration_fingerprint(config)
    assert fingerprint != configuration_fingerprint({**config, "max_total_tokens": 1235})


def test_lifecycle_cost_does_not_count_mirrored_meta_stages_twice():
    stage = {"usage": {"total_tokens": 20}}
    runs = [{"payload": {
        "usage": {"total_tokens": 10},
        "meta_stages": [stage],
        "evaluation": {"meta_stages": [stage], "rows": [
            {"baseline": {"tokens": 3, "model_calls": 2},
             "candidate": {"tokens": 4, "model_calls": 3}},
        ]},
    }}]
    assert lifecycle_tokens(runs) == {
        "reported_tokens": 37, "paired_evaluator_model_calls": 5,
        "failed_proposal_usage_missing": [], "reported_tokens_are_lower_bound": False,
    }


def test_proposal_reliability_counts_repairs_and_failed_meta_stages_once():
    attempts = [{"status": "invalid", "usage": {"total_tokens": 4}},
                {"status": "valid", "usage": {"total_tokens": 6}}]
    stage = {"proposal_attempts": [{"status": "request_error", "usage_complete": False}]}
    report = proposal_reliability([{"payload": {
        "proposal_attempts": attempts, "meta_stages": [stage],
        "evaluation": {"meta_stages": [stage]},
    }}])
    assert report == {"observed_proposals": 2, "first_attempt_valid": 0, "repaired_valid": 1,
                      "final_invalid": 1, "structured_valid": 0, "attempts": 3, "recorded_proposal_tokens": 10,
                      "attempts_with_unknown_usage": 1}


def test_skill_lineage_joins_execution_uuid_and_marks_missing_failure_usage():
    content = "Meta v1 content"
    digest = hashlib.sha256(content.encode()).hexdigest()
    links = task_skill_links({
        "task_outcomes": [{"task_id": "fixture-name", "task": {"task_id": "actual-uuid"},
                           "active_meta_before": {"version_id": "meta-v1",
                                                   "content_sha256": digest}}],
        "task_skill_runs": [summarize_run({"run_id": "r1", "kind": "task", "status": "published",
            "task_id": "actual-uuid", "meta_skill_version_id": "meta-v1",
            "payload": {"base_meta": {"content": content}}})],
    })
    assert links[0]["source_meta_matches_active_before"] is True
    usage = lifecycle_tokens([{"run_id": "failed-run", "status": "failed", "payload": {}}])
    assert usage["failed_proposal_usage_missing"] == ["failed-run"]
    assert usage["reported_tokens_are_lower_bound"] is True


def test_lifecycle_report_summarizes_runs_without_trace_or_skill_contents():
    summary = summarize_run({"run_id": "r1", "kind": "meta", "status": "evaluated",
        "payload": {"traces": [{"prompt": "private prompt"}],
                    "candidate": {"version_id": "v1", "content": "private skill",
                                  "content_sha256": "skill-hash"},
                    "usage": {"total_tokens": 8},
                    "evaluation": {"passed": True, "rows": [{"spec": {"prompt": "private spec"},
                        "baseline": {"passed": True, "tokens": 3},
                        "candidate": {"passed": True, "tokens": 4}}]},
                    "meta_stages": [{"branch": "candidate", "candidate": {
                        "version_id": "v2", "content": "private stage skill",
                        "content_sha256": "stage-hash"}, "usage": {"total_tokens": 5}}]}})
    serialized = json.dumps(summary)

    assert "private prompt" not in serialized
    assert "private spec" not in serialized
    assert "private skill" not in serialized
    assert "private stage skill" not in serialized
    assert "skill-hash" in serialized and "stage-hash" in serialized
    assert summary["evaluation"]["rows"][0]["baseline"]["tokens"] == 3


def test_task_skill_link_uses_builtin_meta_hash_when_no_registry_version_exists():
    content = "built-in Meta guidance"
    digest = hashlib.sha256(content.encode()).hexdigest()
    phase_data = {
        "task_outcomes": [{"task_id": "t1", "active_meta_before": {
            "version_id": None, "source": "builtin", "content_sha256": digest}}],
        "task_skill_runs": [summarize_run({"run_id": "r1", "kind": "task", "status": "published",
            "task_id": "t1", "meta_skill_version_id": None,
            "payload": {"base_meta": {"content": content},
                        "candidate": {"version_id": "skill-v1"},
                        "published_version": {"version_id": "skill-v1"}}})],
    }
    link = task_skill_links(phase_data)[0]
    assert link["source_meta_content_sha256"] == digest
    assert link["source_meta_matches_active_before"] is True


def test_task_skill_link_rejects_registry_source_meta_hash_mismatch():
    active_content = "active Meta guidance"
    stale_content = "stale Meta guidance"
    phase_data = {
        "task_outcomes": [{"task_id": "t1", "active_meta_before": {
            "version_id": "meta-v1", "source": "registry",
            "content_sha256": hashlib.sha256(active_content.encode()).hexdigest()}}],
        "task_skill_runs": [summarize_run({"run_id": "r1", "kind": "task", "status": "published",
            "task_id": "t1", "meta_skill_version_id": "meta-v1",
            "payload": {"base_meta": {"content": stale_content},
                        "candidate": {"version_id": "skill-v1"},
                        "published_version": {"version_id": "skill-v1"}}})],
    }

    link = task_skill_links(phase_data)[0]

    assert link["source_meta_content_sha256"] == hashlib.sha256(stale_content.encode()).hexdigest()
    assert link["source_meta_matches_active_before"] is False


def test_loaded_task_skill_link_requires_verified_injection_and_exact_published_hash():
    links = [{"family": "api_compatibility", "status": "published",
              "published_version": "skill-v1", "published_content_sha256": "hash-v1"},
             {"family": "api_compatibility", "status": "published",
              "published_version": "skill-v2", "published_content_sha256": "hash-v2"},
             {"family": "api_compatibility", "status": "candidate",
              "published_version": "skill-v3", "published_content_sha256": "hash-v3"},
             {"family": "other", "status": "published",
              "published_version": "skill-v4", "published_content_sha256": "hash-v4"}]
    evidence = [
        {"context_id": "verified", "injection_verified": True, "model_call_observed": True,
         "skill_versions": [{"version_id": "skill-v1", "content_sha256": "hash-v1"}]},
        {"context_id": "hash-mismatch", "injection_verified": True, "model_call_observed": True,
         "skill_versions": [{"version_id": "skill-v2", "content_sha256": "stale-hash"}]},
        {"context_id": "injection-unverified", "injection_verified": False, "model_call_observed": True,
         "skill_versions": [{"version_id": "skill-v2", "content_sha256": "hash-v2"}]},
        {"context_id": "no-model-call", "injection_verified": True, "model_call_observed": False,
         "skill_versions": [{"version_id": "skill-v2", "content_sha256": "hash-v2"}]},
    ]

    matched = loaded_task_skill_links(evidence, links, family="api_compatibility")
    assert len(matched) == 1
    assert matched[0]["coder_loaded_version_id"] == "skill-v1"
    assert matched[0]["coder_loaded_content_sha256"] == "hash-v1"
    assert matched[0]["injection_verified"] is True
    assert matched[0]["model_call_observed"] is True
    assert matched[0]["injection_evidence"] == [{
        "version_id": "skill-v1", "content_sha256": "hash-v1", "context_id": "verified",
        "injection_verified": True, "model_call_observed": True,
    }]


def test_heldout_counts_exact_phase_1_publication_load_and_rejects_phase_2_mismatches():
    phase_1_links = [{"family": "api_compatibility", "status": "published",
                      "published_version": "phase-1-skill", "published_content_sha256": "phase-1-hash"}]
    phase_2_links = [{"family": "api_compatibility", "status": "published",
                      "published_version": "phase-2-skill", "published_content_sha256": "phase-2-hash"}]
    evidence = [
        {"context_id": "phase-1-exact", "injection_verified": True, "model_call_observed": True,
         "skill_versions": [{"version_id": "phase-1-skill", "content_sha256": "phase-1-hash"}]},
        {"context_id": "phase-2-stale-hash", "injection_verified": True, "model_call_observed": True,
         "skill_versions": [{"version_id": "phase-2-skill", "content_sha256": "stale-hash"}]},
        {"context_id": "phase-2-no-model-call", "injection_verified": True, "model_call_observed": False,
         "skill_versions": [{"version_id": "phase-2-skill", "content_sha256": "phase-2-hash"}]},
    ]

    loaded = loaded_task_skill_links_by_phase(
        evidence, phase_1_links, phase_2_links, family="api_compatibility")

    assert [link["coder_loaded_version_id"] for link in loaded["phase_1"]] == ["phase-1-skill"]
    assert loaded["phase_1"][0]["coder_loaded_content_sha256"] == "phase-1-hash"
    assert loaded["phase_1"][0]["injection_evidence"][0]["context_id"] == "phase-1-exact"
    assert loaded["phase_2"] == []


def test_compact_exposes_only_coder_skill_injection_evidence_with_both_proofs():
    result = compact({"telemetry": {"skill_contexts": [
        {"role": "coder", "context_id": "accepted", "injection_verified": True,
         "model_call_observed": True,
         "skill_versions": [{"version_id": "skill-v1", "content_sha256": "hash-v1"}]},
        {"role": "coder", "context_id": "missing-model-call", "injection_verified": True,
         "model_call_observed": False,
         "skill_versions": [{"version_id": "skill-v2", "content_sha256": "hash-v2"}]},
        {"role": "coder", "context_id": "unverified", "injection_verified": False,
         "model_call_observed": True,
         "skill_versions": [{"version_id": "skill-v3", "content_sha256": "hash-v3"}]},
        {"role": "reviewer", "context_id": "wrong-role", "injection_verified": True,
         "model_call_observed": True,
         "skill_versions": [{"version_id": "skill-v4", "content_sha256": "hash-v4"}]},
    ]}})

    assert result["coder_skill_injection_evidence"] == [{
        "context_id": "accepted", "injection_verified": True, "model_call_observed": True,
        "skill_versions": [{"version_id": "skill-v1", "content_sha256": "hash-v1"}],
    }]




def test_live_uses_http_harness_and_autonomous_isolated_branches(monkeypatch, tmp_path):
    manifest, materials, held_out = load_dataset()
    calls = []
    fail_frozen_snapshot = False
    active_meta_content = "Meta guidance shared by the fake runtime"
    active_meta_hash = hashlib.sha256(active_meta_content.encode()).hexdigest()

    class TaskExecutionTimedOut(TimeoutError):
        pass

    class FakeInstance:
        def __init__(self, config, *, token_budget_db=None, max_total_tokens=None):
            self.config = config
            self.token_budget_db = token_budget_db
            self.max_total_tokens = max_total_tokens
            self.evolution_reads = 0
            for spec in config.evolution.selection_task_specs:
                assert spec["commit"]
                assert set(spec["protected_acceptance_paths"]) == set(spec["acceptance_files"])
                oracle_path = Path(next(iter(spec["protected_acceptance_paths"].values())))
                assert oracle_path.is_file()
                assert not oracle_path.is_relative_to(Path(spec["repo_path"]))
                assert not (Path(spec["repo_path"]) / spec["acceptance_files"][0]).exists()

        async def __aenter__(self):
            calls.append(("open", self.config.data_dir, self.config.evolution.freeze_meta_skill,
                          self.config.evolution.auto_evaluate,
                          self.config.evolution.auto_publish,
                          len(self.config.evolution.selection_task_specs),
                          self.config.evolution.enabled, self.token_budget_db, self.max_total_tokens))
            return self

        async def __aexit__(self, *_exc):
            calls.append(("close", self.config.data_dir))

        async def run_task(self, workspace, prompt, repository_id):
            assert repository_id.startswith("rsi-prevalidation-")
            assert prompt
            calls.append(("task", self.config.data_dir, workspace.name))
            if ("full_rsi" in self.config.data_dir and "heldout" in workspace.parts and
                    workspace.parent.name == "api_compatibility_02"):
                raise TaskExecutionTimedOut("fake task deadline expired")
            coder_versions = ([] if "no_learning" in self.config.data_dir else
                              ["task-skill-v1"] if "phase_2" in workspace.parts else
                              ["task-skill-v1", "task-skill-phase2"] if "heldout" in workspace.parts else [])
            execution_id = ("uuid-" + materials[workspace.name]["live_task"]["task_id"]
                            if workspace.name in materials else "uuid-" + workspace.name)
            return {"task_id": execution_id, "status": "completed",
                    "events": [{"event_type": "code.patch"}],
                    "telemetry": {"total_tokens": 7,
                                              "skill_contexts": [{"context_id": "fake-coder-context",
                                                  "injection_verified": True, "model_call_observed": True,
                                                  "skill_versions": [
                                                      {"version_id": version_id,
                                                       "content_sha256": "observed-skill-hash"}
                                                      for version_id in coder_versions
                                                  ], "role": "coder"}]}}

        async def wait_idle(self):
            calls.append(("idle", self.config.data_dir))

        async def snapshot(self, destination):
            if (fail_frozen_snapshot and "task_skill_only" in self.config.data_dir and
                    self.config.evolution.freeze_meta_skill):
                raise OSError("simulated snapshot failure")
            destination.mkdir(parents=True)
            (destination / "state.json").write_text(self.config.data_dir)

        async def request(self, method, path, json=None):
            assert method == "GET"
            calls.append(("request", self.config.data_dir, path))
            if path == "/api/config":
                return {"evolution": {"auto_evaluate": True, "auto_publish": True}}
            if path == "/api/evolution":
                self.evolution_reads += 1
                if not self.config.evolution.enabled:
                    return []
                if self.evolution_reads == 1:
                    return []
                runs = [{"run_id": "task-run-1", "kind": "task", "status": "published",
                         "task_id": "uuid-api_material_learning", "meta_skill_version_id": "meta-v0",
                             "payload": {"candidate": {"version_id": "task-skill-v1",
                                                          "content_sha256": "observed-skill-hash"},
                                         "published_version": {"version_id": "task-skill-v1",
                                                                "content_sha256": "observed-skill-hash"},
                                         "base_meta": {"content": active_meta_content}}}]
                if self.evolution_reads >= 3:
                    runs.append({"run_id": "task-run-2", "kind": "task", "status": "published",
                                 "task_id": "uuid-api_material_phase2", "meta_skill_version_id": "meta-v0",
                                     "payload": {"candidate": {"version_id": "task-skill-phase2",
                                                                  "content_sha256": "observed-skill-hash"},
                                                 "published_version": {"version_id": "task-skill-phase2",
                                                                            "content_sha256": "observed-skill-hash"},
                                                     "base_meta": {"content": active_meta_content}}})
                return runs
            if path == "/api/evolution/active-meta":
                return {"version_id": "meta-v0", "source": "registry",
                        "content_sha256": active_meta_hash}
            if path == "/api/skills/versions":
                return [{"skill_id": "s1", "version_id": version_id, "name": "task skill",
                         "level": "task", "content_sha256": "observed-skill-hash", "status": "active"}
                        for version_id in ("task-skill-v1", "task-skill-phase2")]
            raise AssertionError(path)

    def load_live_config():
        return SimpleNamespace(
            data_dir="",
            models=SimpleNamespace(
                local=SimpleNamespace(model_id="local-test", max_context_tokens=100,
                                      max_output_tokens=20),
                evolution=SimpleNamespace(model_id="evolution-test", max_context_tokens=100,
                                          max_output_tokens=20),
            ),
            evolution=SimpleNamespace(enabled=False, auto_trigger_enabled=False,
                                      auto_evaluate=False, auto_publish=True,
                                          freeze_meta_skill=False, selection_task_specs=[],
                                          batch_task_count=8, similar_issue_threshold=3,
                                          meta_review_after_runs=25, timeout=300),
            skills=SimpleNamespace(enabled=False),
            raw_rag=SimpleNamespace(enabled=True, index_when_disabled=True,
                                    background_indexing=True),
            runtime=SimpleNamespace(execution_backend="local", max_turns=40),
        )

    def clone_state(source, destination):
        destination.mkdir(parents=True)
        (destination / "state.json").write_bytes((source / "state.json").read_bytes())
        calls.append(("clone", str(source), str(destination)))

    async def protected_acceptance(config, workspace, oracle_root, filename, source, command):
        assert config.runtime.execution_backend == "docker"
        assert filename and source and command[:3] == ["python", "-I", "-B"]
        assert not (workspace / filename).exists()
        return {"passed": True, "acceptance_protected": True,
                "acceptance_file_sha256": {filename: hashlib.sha256(source.encode()).hexdigest()}}

    import sys
    import scripts
    module = SimpleNamespace(Instance=FakeInstance, load_live_config=load_live_config,
                             clone_state=clone_state, protected_acceptance=protected_acceptance,
                             TaskExecutionTimedOut=TaskExecutionTimedOut)
    monkeypatch.setitem(sys.modules, "scripts.blackbox_runtime", module)
    # `from scripts import blackbox_runtime` may resolve a cached package attribute
    # after another test imported the real runtime module. Replace both lookup paths.
    monkeypatch.setattr(scripts, "blackbox_runtime", module, raising=False)
    report = asyncio.run(live_rsi_prevalidation.live(
        family="api_compatibility", max_total_tokens=1_500_000))

    assert report["status"] == "completed", report
    assert report["gates"]["all_phase_tasks_completed_with_protected_acceptance"] is True
    assert report["family_filter"] == "api_compatibility"
    assert report["families"] == ["api_compatibility"]
    assert len(report["heldout_diagnostic"]["paired_outcomes"]) == 6
    completed = [row for row in report["heldout_diagnostic"]["paired_outcomes"]
                 if row["task"].get("status") == "completed"]
    assert all(row["task"]["coder_skill_hashes"] == ["observed-skill-hash"] for row in completed
               if row["branch"] != "no_learning")
    assert all(row["task"]["coder_skill_hashes"] == [] for row in completed
               if row["branch"] == "no_learning")
    assert report["lifecycle"]["task_skill_only"]["phase_1_boundary"]["active_meta"]["version_id"] == "meta-v0"
    phase_1_link = report["lifecycle"]["task_skill_only"]["phase_1_boundary"]["task_skill_links"][0]
    assert phase_1_link["source_meta_skill_version_id"] == "meta-v0"
    assert phase_1_link["task_skill_version_id"] == "task-skill-v1"
    assert phase_1_link["published_version"] == "task-skill-v1"
    assert phase_1_link["source_meta_matches_active_before"] is True
    assert report["threshold_profile"] == "mechanism_prevalidation_not_production"
    assert all(entry[4] is False for entry in calls if entry[0] == "open" and "no_learning" in entry[1])
    assert all(entry[4] is True for entry in calls if entry[0] == "open" and
               "task_skill_only" in entry[1] and "heldout" not in entry[1])
    assert all(entry[4] is True for entry in calls if entry[0] == "open" and
               "full_rsi" in entry[1] and "heldout" not in entry[1])
    assert {entry[2] for entry in calls if entry[0] == "request"} == {
        "/api/config", "/api/evolution", "/api/skills/versions", "/api/evolution/active-meta"}
    assert len([entry for entry in calls if entry[0] == "clone"]) == 6
    assert report["heldout_diagnostic"]["pass_rates"]["full_rsi"]["solved"] == 1
    assert report["heldout_diagnostic"]["pass_rates"]["full_rsi"]["agent_timeouts"] == 1
    assert set(report["heldout_diagnostic"]["pairwise_comparisons"]) == {
        "no_learning_vs_task_skill_only", "no_learning_vs_full_rsi", "task_skill_only_vs_full_rsi"}
    assert report["heldout_diagnostic"]["runner_failures"] == 0
    assert report["costs"]["phase_1"]["task_skill_only"]["agent_attempts"] == 2
    assert report["costs"]["phase_2"]["task_skill_only"]["agent_attempts"] == 2
    phase_2_task = report["phases"]["task_skill_only"]["phase_2"]["task_outcomes"][0]
    assert phase_2_task["active_meta_before"]["version_id"] == "meta-v0"
    assert phase_2_task["task"]["coder_skill_versions"] == ["task-skill-v1"]
    assert len(phase_2_task["loaded_phase_1_task_skill_links"]) == 1
    assert phase_2_task["loaded_phase_1_task_skill_links"][0]["coder_loaded_version_id"] == "task-skill-v1"
    phase_2_link = report["lifecycle"]["task_skill_only"]["phase_2_task_skill_links"][0]
    assert phase_2_link["source_meta_skill_version_id"] == "meta-v0"
    assert phase_2_link["published_version"] == "task-skill-phase2"
    assert phase_2_link["source_meta_matches_active_before"] is True
    full_rsi_gate = report["gates"]["mechanism_gates"]["full_rsi"]
    assert full_rsi_gate["passed"] is False
    assert full_rsi_gate["meta_changed"] is False
    heldout = next(row for row in report["heldout_diagnostic"]["paired_outcomes"]
                   if row["branch"] == "task_skill_only" and row["task_id"] == "api_compatibility_01")
    assert heldout["used_published_phase_2_task_skill"] is True
    assert heldout["loaded_phase_2_task_skill_links"][0]["coder_loaded_version_id"] == "task-skill-phase2"
    assert report["baseline_models_and_budgets"]["local_model_id"] == "local-test"
    open_calls = [entry for entry in calls if entry[0] == "open"]
    assert open_calls and {entry[6] for entry in open_calls} == {False, True}
    assert {branch for branch in ("no_learning", "task_skill_only", "full_rsi")
            if any(branch in entry[1] for entry in open_calls)} == {
                "no_learning", "task_skill_only", "full_rsi"}
    assert all(entry[7] for entry in open_calls)
    assert {entry[8] for entry in open_calls} == {1_500_000}
    assert all("no_learning" in entry[1] and entry[6] is False
               for entry in open_calls if "no_learning" in entry[1])
    assert all("task_skill_only" in entry[1] and entry[6] is True and entry[2] is True
               for entry in open_calls if "task_skill_only" in entry[1] and "heldout" not in entry[1])
    assert all("full_rsi" in entry[1] and entry[6] is True and entry[2] is False
               for entry in open_calls if "full_rsi" in entry[1] and "heldout" not in entry[1])
    assert not any("trigger" in str(entry).lower() or "publish" in str(entry).lower()
                   for entry in calls if entry[0] in {"request", "task"})

    starts_by_branch = {
        branch: sum(entry[0] == "task" and "heldout" in entry[1] and branch in entry[1]
                    for entry in calls)
        for branch in ("no_learning", "task_skill_only", "full_rsi")
    }
    assert {branch: report["heldout_diagnostic"]["pass_rates"][branch]["attempted"]
            for branch in starts_by_branch} == starts_by_branch

    fail_frozen_snapshot = True
    prior_call_count = len(calls)
    partial = asyncio.run(live_rsi_prevalidation.live(
        family="api_compatibility", max_total_tokens=1_500_000))
    diagnostic = partial["heldout_diagnostic"]
    assert diagnostic["accounted_outcomes"] == diagnostic["planned_outcomes"] == 6
    assert diagnostic["runner_failures"] == 2
    assert diagnostic["pass_rates"]["full_rsi"]["solved"] == 1
    assert diagnostic["pairwise_comparisons"]["no_learning_vs_task_skill_only"]["unscored_pairs"] == 2
    new_calls = calls[prior_call_count:]
    partial_starts_by_branch = {
        branch: sum(entry[0] == "task" and "heldout" in entry[1] and branch in entry[1]
                    for entry in new_calls)
        for branch in ("no_learning", "task_skill_only", "full_rsi")
    }
    assert {branch: diagnostic["pass_rates"][branch]["attempted"]
            for branch in partial_starts_by_branch} == partial_starts_by_branch
