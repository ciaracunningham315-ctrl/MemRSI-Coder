"""Offline formal runner contracts; never constructs or calls LiveBackend."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.chain import atomic_json, digest, read_json
from runner.formal import (FormalRunner, FrozenNSRRAuditReader, formal_execution_schedule,
    summarize_nsrr, validate_formal_structure, validate_gate_regression_matrix,
    validate_nsrr_gate_decisions)


class OfflineFormalBackend:
    offline = True

    def __init__(self):
        self.calls = []
        self.collections = {}
        self.task_wave_events = {}
        self.task_wave_started = {}
        self.active_task_waves = set()
        self.accepted_tasks = {}
        self.task_dispatch_count = 0
        self.fail_once_after_accepting = None
        self.learning_prepare_events = {}
        self.learning_prepare_members = {}
        self.learning_evaluation_events = {}
        self.learning_evaluation_members = {}
        self.learning_evaluated = {}
        self.batch_records = []

    def budget(self):
        return {"stop_reason": None, "remaining_admissible_tokens": 1_000_000, "request_count": 0}

    def read(self, state):
        return read_json(state / "state.json")

    def write(self, state, value):
        atomic_json(state / "state.json", value)

    def state_fingerprint(self, state):
        return digest(self.read(state))

    @asynccontextmanager
    async def task_batch(self, entries):
        unique_apps = {(str(Path(item["state"]).resolve()), item["condition"], item["collection"])
                       for item in entries}
        record = {"entries": copy.deepcopy(entries), "unique_application_count": len(unique_apps),
            "state_paths": sorted({str(Path(item["state"]).resolve()) for item in entries})}
        self.batch_records.append(record)
        self.calls.append(("task_batch", record["unique_application_count"], record["state_paths"]))
        yield

    async def initialize(self, state, collection):
        state.mkdir(parents=True, exist_ok=True)
        self.write(state, {"tasks": [], "indexed": [], "skills": [], "meta": "V1"})
        self.collections[collection] = []
        self.calls.append(("initialize", collection))
        return {"collection": collection, "history": {"task_ids": [], "event_ids": []},
                "task_skills": [], "meta": "V1"}

    def clone(self, source, destination):
        if not destination.exists():
            shutil.copytree(source, destination)
        self.calls.append(("clone", str(source), str(destination)))
        return {"source_sha256": self.state_fingerprint(source)}

    async def audit(self, state, descriptor):
        data = self.read(state)
        assert self.collections[descriptor["collection"]] == data["indexed"]
        actual = digest(data["indexed"])
        if descriptor.get("content_sha256"):
            assert descriptor["content_sha256"] == actual
        return {**descriptor, "content_sha256": actual}

    async def task(self, state, condition, history, collection, workspace, prompt, manifest,
                   save_identity, repository_id="repo_api"):
        stage = manifest["stage"]
        workspace_key = str(workspace.resolve())
        accepted = self.accepted_tasks.get(workspace_key)
        if accepted is not None:
            self.calls.append(("task_recovered", condition, repository_id, stage, accepted["task_id"]))
            save_identity(accepted["task_id"])
            workspace.mkdir(parents=True, exist_ok=True)
            return copy.deepcopy(accepted)
        wave_key = ("A", stage) if condition in {"A1", "A2"} else (condition, stage)
        expected = 6 if condition in {"A1", "A2"} else 3
        event = self.task_wave_events.setdefault(wave_key, asyncio.Event())
        members = self.task_wave_started.setdefault(wave_key, [])
        initial = self.read(state)
        task_id = "offline_" + workspace.name
        members.append({"condition": condition, "repository_id": repository_id, "stage": stage,
            "history": copy.deepcopy(history), "collection": collection,
            "indexed_sha256": digest(initial["indexed"]), "state_path": str(state),
            "state_before_sha256": digest(initial)})
        self.calls.append(("task_start", condition, repository_id, stage, task_id,
            copy.deepcopy(history), digest(initial["indexed"]), str(state), digest(initial)))
        if len(members) == expected:
            event.set()
        assert len(members) <= expected, (wave_key, len(members), expected)
        self.active_task_waves.add(wave_key)
        await event.wait()

        data = self.read(state)
        assert set(history["task_ids"]) <= set(data["tasks"])
        if task_id not in data["tasks"]:
            data["tasks"].append(task_id)
            self.write(state, data)
        output = {"task_id": task_id, "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "completed", "termination": "completed",
            "events": [{"event_id": task_id + "_event", "task_id": task_id,
                        "event_type": "task.completed"}],
            "telemetry": {"model_calls": 0, "reported_tokens": None, "tokens_complete": False}}
        self.accepted_tasks[workspace_key] = copy.deepcopy(output)
        self.task_dispatch_count += 1
        self.calls.append(("task_dispatch", condition, repository_id, stage, task_id))
        save_identity(task_id)
        workspace.mkdir(parents=True, exist_ok=True)
        self.calls.append(("task_terminal", condition, repository_id, stage, task_id))
        if self.fail_once_after_accepting == workspace.name:
            self.fail_once_after_accepting = None
            self.active_task_waves.discard(wave_key)
            raise RuntimeError("offline simulated runner interruption after Agent accepted task")
        self.active_task_waves.discard(wave_key)
        return copy.deepcopy(output)

    async def boundary(self, state, condition, history, old_collection, new_collection):
        assert not any(wave[0] == condition for wave in self.active_task_waves), "index crossed an Agent task barrier"
        data = self.read(state)
        data["indexed"] = list(history["event_ids"])
        self.write(state, data)
        self.collections[new_collection] = list(data["indexed"])
        self.calls.append(("index", condition, new_collection, copy.deepcopy(history)))
        return {"collection": new_collection, "history": history,
                "content_sha256": digest(data["indexed"]), "points_count": len(data["indexed"])}

    async def prepare_learning(self, state, condition, history, descriptor, ids, kind, action_key, stage,
                               specs, repository_id=None, meta_history_run_ids=None):
        assert ids and set(ids) <= set(history["task_ids"])
        task_scopes = history.get("task_ids_by_repository", {})
        if kind == "task":
            assert repository_id and set(ids) <= set(task_scopes[repository_id])
        if kind == "meta":
            assert repository_id is None and condition == "B4" and stage == "transfer"
            assert len(ids) == 6
        wave = (condition, stage, kind)
        expected = 3 if kind == "task" else 1
        event = self.learning_prepare_events.setdefault(wave, asyncio.Event())
        members = self.learning_prepare_members.setdefault(wave, [])
        data = self.read(state)
        snapshot = {"active_skills": list(data["skills"]), "active_meta": data["meta"]}
        snapshot["sha256"] = digest(snapshot)
        members.append({"repository_id": repository_id, "ids": list(ids), "snapshot": copy.deepcopy(snapshot)})
        self.calls.append(("prepare_start", condition, kind, repository_id, stage, list(ids),
            [item["task_id"] for item in specs], snapshot["sha256"], list(meta_history_run_ids or [])))
        if len(members) == expected:
            event.set()
        assert len(members) <= expected
        await event.wait()
        run_id = action_key.replace(":", "_")
        version_id = run_id + "_candidate"
        output = {"run_id": run_id, "candidate_version_id": version_id,
            "status": "candidate", "proposal_snapshot": snapshot,
            "meta_history_run_ids": list(meta_history_run_ids or [])}
        self.calls.append(("prepared", condition, kind, repository_id, stage, run_id, snapshot["sha256"]))
        return output

    async def evaluate_learning(self, state, condition, history, descriptor, prepared, specs, kind,
                                repository_id=None):
        run_id = prepared["run_id"]
        stage = specs[0].get("stage", "transfer")
        # The action payload does not necessarily repeat stage; infer it from the run key.
        stage = "transfer" if "transfer" in run_id else ("stale" if "stale" in run_id else "seed")
        wave = (condition, stage, kind)
        expected = 3 if kind == "task" else 1
        event = self.learning_evaluation_events.setdefault(wave, asyncio.Event())
        members = self.learning_evaluation_members.setdefault(wave, [])
        assert not any(call[0] == "publish" and call[1] == condition and call[2] == kind and call[4] == stage
                       for call in self.calls), "publish occurred before all Gate evaluations were dispatched"
        members.append(prepared["run_id"])
        self.calls.append(("evaluate_start", condition, kind, repository_id, stage, run_id))
        if len(members) == expected:
            event.set()
        assert len(members) <= expected
        await event.wait()
        self.learning_evaluated.setdefault(wave, set()).add(run_id)
        output = {**prepared, "status": "evaluated", "evaluation_passed": True}
        self.calls.append(("evaluated", condition, kind, repository_id, stage, run_id))
        return output

    async def publish_learning(self, state, condition, history, descriptor, evaluation, kind,
                               repository_id=None):
        run_id = evaluation["run_id"]
        stage = "transfer" if "transfer" in run_id else ("stale" if "stale" in run_id else "seed")
        wave = (condition, stage, kind)
        expected = 3 if kind == "task" else 1
        assert len(self.learning_evaluated.get(wave, set())) == expected, "publish crossed Gate evaluation barrier"
        data = self.read(state)
        if evaluation.get("evaluation_passed"):
            if kind == "meta":
                data["meta"] = run_id + "_published"
            else:
                data["skills"].append({"repository_id": repository_id, "version_id": evaluation["candidate_version_id"]})
            self.write(state, data)
        self.calls.append(("publish", condition, kind, repository_id, stage, run_id,
            list(evaluation.get("trace_task_ids", []))))
        return {**evaluation, "status": "published" if evaluation.get("evaluation_passed") else "needs_review"}

    async def learn(self, state, condition, history, descriptor, ids, kind, action_key, stage,
                    specs, repository_id=None):
        prepared = await self.prepare_learning(state, condition, history, descriptor, ids,
            kind, action_key, stage, specs, repository_id=repository_id)
        evaluated = await self.evaluate_learning(state, condition, history, descriptor,
            prepared, specs, kind, repository_id=repository_id)
        return await self.publish_learning(state, condition, history, descriptor, evaluated,
            kind, repository_id=repository_id)

    async def history_evidence(self, state, task_ids, workspace, repository_id="repo_api"):
        return {"source": "offline fixture", "repository_id": repository_id,
                "task_ids": list(task_ids), "evidence_present": False}


def offline_grader(root, manifest, artifact, output):
    async def result(*_args):
        # An offline fake must not manufacture a positive or valid private grade.
        return {"status": "unverified", "solved": None, "reason": "offline fake backend"}
    return result


def copy_benchmark(tmp_path):
    root = tmp_path / "benchmark"
    root.mkdir(parents=True)
    for name in ("scenarios", "git_stage_packages", "gate_cases"):
        shutil.copytree(ROOT / name, root / name)
    (root / "configs").mkdir()
    shutil.copy2(ROOT / "configs/gate_task_catalog.json", root / "configs/gate_task_catalog.json")
    (root / "tests").mkdir(exist_ok=True)
    protocol = yaml.safe_load((ROOT / "configs/protocol.yaml").read_text())
    audit_ids = []
    for candidate_file in sorted((ROOT / "gate_cases").glob("*/candidate.json")):
        audit_ids.append(json.loads(candidate_file.read_text())["audit_task_id"])
    protocol["profiles"]["formal_benchmark"]["nsrr"]["audit_task_ids"] = audit_ids
    (root / "configs/protocol.yaml").write_text(yaml.safe_dump(protocol, sort_keys=False))
    for private_name in ("acceptance", "regression", "references", "gate_selection", "regression_replay", "gate_audit"):
        source = ROOT / "private" / private_name
        if source.exists():
            (root / "private").mkdir(exist_ok=True)
            shutil.copytree(source, root / "private" / private_name)
    for name in ("runner", "scripts", "tests"):
        (root / name).mkdir(exist_ok=True)
    for relative in ("runner/nsrr_audit_tasks.py", "scripts/run_nsrr_audit.py",
                     "scripts/qualify_nsrr_audit_fixtures.py", "tests/test_nsrr_audit.py",
                     "tests/test_nsrr_audit_fixture_qualification.py"):
        shutil.copy2(ROOT / relative, root / relative)
    return root


def offline_gate_provider(root, stage, kind, runtime, repository_id=None):
    catalog = json.loads((root / "configs/gate_task_catalog.json").read_text())
    from runner.gate_tasks import _selected_ids
    ids = _selected_ids(root, stage, kind, repository_id)
    scopes = catalog.get("repository_ids", {})
    return [{"task_id": task_id, "repository_id": scopes[task_id]} for task_id in ids]


def test_formal_schedule_is_exact_and_protocol_ordered():
    protocol = yaml.safe_load((ROOT / "configs/protocol.yaml").read_text())
    assert validate_formal_structure(protocol) == []
    schedule = formal_execution_schedule(ROOT, protocol)
    assert len(schedule) == 54
    assert len({row["attempt_id"] for row in schedule}) == 54
    assert sum(row["experiment"] == "A" for row in schedule) == 18
    assert sum(row["experiment"] == "B" for row in schedule) == 36
    assert not any(row["experiment"] == "A" and row["stage_name"] == "seed" for row in schedule)
    for stage_name in protocol["experiment_a"]["scored_stages"]:
        rows = [row for row in schedule if row["experiment"] == "A" and row["stage_name"] == stage_name]
        assert [row["repository_id"] for row in rows[:3]] == ["repo_api", "repo_cli", "repo_data"]
        assert [row["repository_id"] for row in rows[3:]] == ["repo_api", "repo_cli", "repo_data"]
    for condition in ("B2", "B3", "B4"):
        rows = [row for row in schedule if row["condition"] == condition]
        assert [row["stage_name"] for row in rows] == ["seed"] * 3 + ["transfer"] * 3 + ["stale"] * 3 + ["heldout"] * 3
        assert [row["repository_id"] for row in rows[:3]] == ["repo_api", "repo_cli", "repo_data"]


def test_gate_matrix_checks_repository_and_regression_scope():
    protocol = yaml.safe_load((ROOT / "configs/protocol.yaml").read_text())
    result = validate_gate_regression_matrix(ROOT, protocol)
    assert result["valid"], result["errors"]
    assert result["matrix"]["regression"] == {
        "repo_api": ["repo_api_replay_status"],
        "repo_cli": ["repo_cli_replay_profile_summary"],
        "repo_data": ["repo_data_replay_timecard_rounding"],
    }
    broken = copy.deepcopy(protocol)
    broken["profiles"]["formal_benchmark"]["regression_task_ids"]["repo_cli"] = ["repo_api_replay_status"]
    result = validate_gate_regression_matrix(ROOT, broken)
    assert not result["valid"]
    assert any("repo_cli" in error for error in result["errors"])


def test_nsrr_reader_reproduces_frozen_external_labels_without_gate_claims(tmp_path):
    audit_ids = sorted(json.loads(path.read_text())["audit_task_id"]
                       for path in (ROOT / "gate_cases").glob("*/candidate.json"))
    rows = FrozenNSRRAuditReader().read(ROOT, audit_ids)
    assert len(rows) == 8
    assert {row["observed_label"] for row in rows} == {"neutral", "harmful", "beneficial", "unknown"}
    summary = summarize_nsrr(rows, [])
    assert summary["status"] == "unverified"
    assert summary["value"] is None
    assert summary["N"] == 6
    assert summary["unknown_label_count"] == 1
    assert summary["matrix_status"] == "unverified"
    assert summary["candidate_types_are_labels"] is False
    with pytest.raises(ValueError, match="independent candidate-evaluation API"):
        validate_nsrr_gate_decisions([{"candidate_id": rows[0]["candidate_id"], "run_id": "x",
            "status": "evaluated", "decision": "blocked", "evaluation_passed": False,
            "source": "main_learning_journal"}])

    copied = copy_benchmark(tmp_path / "tamper")
    audit_file = copied / "private/gate_audit/observed_labels.json"
    changed = json.loads(audit_file.read_text())
    changed["observed_labels"][0]["observed_label"] = "harmful"
    audit_file.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="do not match frozen deterministic audit"):
        FrozenNSRRAuditReader().read(copied, audit_ids)


def test_nsrr_adverse_kn_is_independent_of_unknown_and_beneficial_records():
    audit_ids = sorted(json.loads(path.read_text())["audit_task_id"]
                       for path in (ROOT / "gate_cases").glob("*/candidate.json"))
    rows = FrozenNSRRAuditReader().read(ROOT, audit_ids)
    adverse = [row for row in rows if row["observed_label"] in {"harmful", "neutral"}]
    records = [{"candidate_id": row["candidate_id"], "batch_id": "nsrr_test", "run_id": "run_" + row["candidate_id"],
        "evaluation_id": "eval_" + row["candidate_id"], "status": "evaluated",
        "source": "nsrr_candidate_evaluation_api_record", "decision": "accepted",
        "evaluation_passed": True, "evaluation": {"passed": True}}
        for row in adverse]
    records.extend({"candidate_id": row["candidate_id"], "batch_id": "nsrr_test",
        "run_id": "failed_" + row["candidate_id"], "status": "failed", "error": "offline fake failure"}
        for row in rows if row["observed_label"] not in {"harmful", "neutral"})
    summary = summarize_nsrr(rows, records)
    assert summary["status"] == "calculated"
    assert summary["value"] == 0
    assert summary["K"] == 0 and summary["N"] == 6
    assert summary["adverse_decisions_complete"] is True
    assert summary["beneficial_decisions_complete"] is False
    assert summary["beneficial_gate_accept_count"] is None
    assert summary["unknown_label_count"] == 1
    assert summary["matrix_status"] == "complete"
    assert summary["terminal_candidate_count"] == 8


def test_nsrr_live_adapter_uses_frozen_skills_private_fixture_and_scoped_roots(tmp_path, monkeypatch):
    from runner.nsrr_gate_api import LiveNSRRGateEvaluator

    root = copy_benchmark(tmp_path)
    audit_ids = sorted(json.loads(path.read_text())["audit_task_id"]
                       for path in (ROOT / "gate_cases").glob("*/candidate.json"))
    rows = FrozenNSRRAuditReader().read(root, audit_ids)
    observed = {}

    class FakeAPI:
        def __init__(self):
            self.body = None

        async def request(self, method, path, json=None):
            if method == "POST":
                assert path == "/api/evolution/external-candidates/evaluate"
                self.body = json
                return {"batch_id": "offline_nsrr", "status": "queued"}
            assert method == "GET" and path == "/api/evolution/external-candidates/offline_nsrr"
            return {"batch_id": "offline_nsrr", "status": "failed", "records": [
                {"candidate_id": candidate["candidate_id"], "run_id": "failed_" + candidate["candidate_id"],
                 "status": "failed", "error": "offline fake evaluator"}
                for candidate in self.body["candidates"]]}

    class FakeBackend:
        def __init__(self):
            self.batch_dir = root / "results" / "offline_nsrr"

        async def create_empty_collection(self, collection):
            observed["collection"] = collection

        @asynccontextmanager
        async def instance(self, state, condition, history, collection, repository_id):
            observed["child_env"] = {key: os.environ.get(key) for key in (
                "EVOCODE_NSRR_AUDIT_ROOT", "EVOCODE_NSRR_FIXTURE_ROOT")}
            observed["instance"] = (str(state), condition, history, collection, repository_id)
            yield api

    api = FakeAPI()
    monkeypatch.setenv("EVOCODE_NSRR_AUDIT_ROOT", "outside_audit_root")
    monkeypatch.setenv("EVOCODE_NSRR_FIXTURE_ROOT", "outside_fixture_root")
    evaluator = LiveNSRRGateEvaluator(root, backend=FakeBackend(), poll_interval=0)
    request = asyncio.run(evaluator.evaluate(root, "offline_nsrr", rows))

    body = api.body
    assert request["status"] == "failed"
    assert body["audit_fixture_root"] == str((root / ".runtime/nsrr_fixtures").resolve())
    assert len(body["candidates"]) == 8
    assert observed["child_env"] == {
        "EVOCODE_NSRR_AUDIT_ROOT": str(root.resolve()),
        "EVOCODE_NSRR_FIXTURE_ROOT": str((root / ".runtime/nsrr_fixtures").resolve()),
    }
    assert os.environ["EVOCODE_NSRR_AUDIT_ROOT"] == "outside_audit_root"
    assert os.environ["EVOCODE_NSRR_FIXTURE_ROOT"] == "outside_fixture_root"
    for candidate in body["candidates"]:
        assert len(candidate["task_specs"]) == 1
        spec = candidate["task_specs"][0]
        assert spec["repo_path"] == str((root / ".runtime/nsrr_fixtures" / spec["audit_task_id"]).resolve())
        assert spec["commit"] == read_json(root / f"private/gate_audit/tasks/{spec['audit_task_id']}/fixture_manifest.json")["commit"]
        assert set(spec["protected_acceptance_paths"]) == set(spec["acceptance_files"])
        assert all(Path(path).is_file() and not Path(path).is_relative_to(Path(spec["repo_path"]))
                   for path in spec["protected_acceptance_paths"].values())
    serialized = json.dumps(body, ensure_ascii=False)
    assert "policy.json" not in serialized
    assert "assume_utc" not in serialized and "honor_offset" not in serialized


def test_offline_formal_runner_records_all_states_and_barriers(tmp_path, monkeypatch):
    import pytest
    root = copy_benchmark(tmp_path)
    # Keep tests independent of LiveBackend even if a future default changes.
    import runner.chain as chain_module
    monkeypatch.setattr(chain_module, "LiveBackend", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LiveBackend called")), raising=False)
    backend = OfflineFormalBackend()
    grader = offline_grader(root, {}, {}, {})
    import runner.formal as formal_module
    import runner.stage_packages as package_module
    def materialize_fixture(_root, manifest, destination):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "offline_fixture.py").write_text(manifest["repository_id"] + ":" + manifest["stage"])
        return {"offline_fixture": True, "repository_id": manifest["repository_id"], "stage": manifest["stage"]}
    def collect_fixture(_workspace, _manifest, destination, **_kwargs):
        return {"inventory": [], "inventory_sha256": "offline", "patch_sha256": "offline",
            "artifact_dir": str(destination), "complete_patch": str(destination / "complete.patch")}
    monkeypatch.setattr(formal_module, "validate_stage_package", lambda *_args, **_kwargs: {"offline_fixture": True})
    monkeypatch.setattr(chain_module, "materialize_stage_package", materialize_fixture)
    monkeypatch.setattr(package_module, "materialize_stage_package", materialize_fixture)
    monkeypatch.setattr(chain_module, "collect_artifact", collect_fixture)
    runner = FormalRunner(root, "offline_formal", backend, gate_provider=offline_gate_provider,
        grader=grader, require_ready=False)
    summary = asyncio.run(runner.run())
    assert summary["planned"] == 54 and summary["recorded"] == 54
    assert summary["missing_count"] == 0
    assert summary["formal_results"] is False and summary["offline_contract_test"] is True
    assert summary["coverage"] == 0
    assert len(read_json(runner.results / "planned_matrix.json")) == 54
    assert len(read_json(runner.results / "attempt_states.json")) == 54
    assert read_json(runner.results / "missing_ledger.json") == []
    assert summary["nsrr"]["status"] == "unverified" and summary["nsrr"]["value"] is None

    task_publishes = [row for row in backend.calls if row[0] == "publish" and row[2] == "task"]
    meta_preparations = [row for row in backend.calls if row[0] == "prepare_start" and row[2] == "meta"]
    assert len(task_publishes) == 18  # 3 repos x 3 learning boundaries x B3/B4
    assert len(meta_preparations) == 1
    b4_t2_publishes = [row for row in task_publishes if row[1] == "B4" and row[4] == "transfer"]
    assert [row[3] for row in b4_t2_publishes] == ["repo_api", "repo_cli", "repo_data"]
    meta_index = backend.calls.index(meta_preparations[0])
    assert all(backend.calls.index(row) < meta_index for row in b4_t2_publishes)
    assert len(meta_preparations[0][5]) == 6
    assert meta_preparations[0][8] == [
        f"offline_formal_B4_{stage}_task_{repository_id}"
        for stage in ("seed", "transfer") for repository_id in ("repo_api", "repo_cli", "repo_data")]
    meta_trace_ids = meta_preparations[0][5]
    task_created = {output["task_id"]: output["created_at"] for output in backend.accepted_tasks.values()}
    repo_order = {repository_id: index for index, repository_id in enumerate(("repo_api", "repo_cli", "repo_data"))}
    def trace_order_key(task_id):
        repository_id = "repo_" + task_id.split("_")[3]
        created = datetime.fromisoformat(task_created[task_id].replace("Z", "+00:00")).astimezone(timezone.utc)
        return created, repo_order[repository_id], task_id
    assert meta_trace_ids == sorted(meta_trace_ids, key=trace_order_key)

    # Every task wave observes one shared stage-start history/index. All tasks
    # reach terminal before the single index call, whose merged IDs follow the
    # fixed repository order even if the fake task durations differ.
    task_starts = [row for row in backend.calls if row[0] == "task_start"]
    for condition in ("collector", "B2", "B3", "B4"):
        for stage in ("seed", "transfer", "stale"):
            group = [row for row in task_starts if row[1] == condition and row[3] == stage]
            assert len(group) == 3
            assert len({digest(row[5]) for row in group}) == 1
            assert len({row[6] for row in group}) == 1
            terminal = [row for row in backend.calls if row[0] == "task_terminal" and row[1] == condition and row[3] == stage]
            index = [row for row in backend.calls if row[0] == "index" and row[1] == condition
                     and row[2].endswith({"seed": "t1_seed", "transfer": "t2_transfer", "stale": "t3_stale"}[stage])]
            if condition == "collector" or condition in ("B2", "B3", "B4"):
                assert len(terminal) == 3 and len(index) == 1
                assert max(backend.calls.index(row) for row in terminal) < backend.calls.index(index[0])
                prior = [f"offline_{condition}_{repo}_{prior_stage}"
                    for prior_stage in ("seed", "transfer", "stale")[:("seed", "transfer", "stale").index(stage)]
                    for repo in ("repo_api", "repo_cli", "repo_data")]
                expected = prior + [f"offline_{condition}_{repo}_{stage}"
                    for repo in ("repo_api", "repo_cli", "repo_data")]
                assert index[0][3]["task_ids"] == expected

    # All three RSI proposals are prepared before evaluation, all three Gate
    # evaluations return before the fixed-order publication phase.
    for condition in ("B3", "B4"):
        for stage in ("seed", "transfer", "stale"):
            prepare = [row for row in backend.calls if row[0] == "prepare_start" and row[1] == condition
                       and row[2] == "task" and row[4] == stage]
            evaluated = [row for row in backend.calls if row[0] == "evaluated" and row[1] == condition
                         and row[2] == "task" and row[4] == stage]
            evaluate_start = [row for row in backend.calls if row[0] == "evaluate_start" and row[1] == condition
                              and row[2] == "task" and row[4] == stage]
            publish = [row for row in backend.calls if row[0] == "publish" and row[1] == condition
                       and row[2] == "task" and row[4] == stage]
            assert len(prepare) == len(evaluate_start) == len(evaluated) == len(publish) == 3
            assert len({row[7] for row in prepare}) == 1  # identical active Skill/Meta snapshot
            assert max(backend.calls.index(row) for row in prepare) < min(backend.calls.index(row) for row in evaluate_start)
            assert max(backend.calls.index(row) for row in evaluate_start) < min(backend.calls.index(row) for row in evaluated)
            assert max(backend.calls.index(row) for row in evaluated) < min(backend.calls.index(row) for row in publish)
            assert [row[3] for row in publish] == ["repo_api", "repo_cli", "repo_data"]

    t4 = [row for row in backend.calls if row[0] == "task_start" and row[1] in ("B2", "B3", "B4") and row[3] == "heldout"]
    assert len(t4) == 9
    for condition in ("B2", "B3", "B4"):
        group = [row for row in t4 if row[1] == condition]
        assert [row[2] for row in group] == ["repo_api", "repo_cli", "repo_data"]
    assert not any(row[0] == "task_start" and row[1] in ("A1", "A2") and row[3] == "seed" for row in backend.calls)

    pairs = summary["concurrency"]["a_pairs"]
    assert len(pairs) == 9 and all(pair["complete_pair"] for pair in pairs)
    for pair in pairs:
        assert pair["pair_arm_order"] in (["A1", "A2"], ["A2", "A1"])
        assert [item["condition"] for item in pair["attempts"]] == pair["pair_arm_order"]
        assert pair["start_skew_seconds"] is not None
        pair_starts = [row for row in task_starts if row[1] in ("A1", "A2")
            and Path(row[7]).name in {item["attempt_id"] for item in pair["attempts"]}]
        assert len(pair_starts) == 2
        assert pair_starts[0][5] == pair_starts[1][5]
        assert pair_starts[0][6] == pair_starts[1][6]
        assert pair_starts[0][7] != pair_starts[1][7]
        assert pair_starts[0][8] == pair_starts[1][8]

    # The fake task_batch observes one Application per shared B condition DB;
    # A arms and T4 task clones retain independent state paths.
    task_batch_records = backend.batch_records
    a_batches = [row for row in task_batch_records if len(row["entries"]) == 6]
    assert len(a_batches) == 3 and all(row["unique_application_count"] == 6 for row in a_batches)
    b_stage_batches = [row for row in task_batch_records if len(row["entries"]) == 3
        and len({entry["condition"] for entry in row["entries"]}) == 1
        and row["entries"][0]["condition"] in ("B2", "B3", "B4")]
    assert all(row["unique_application_count"] == (3 if any("/heldout/" in path for path in row["state_paths"]) else 1)
               for row in b_stage_batches)
    live_state_paths = {}
    for condition in ("B2", "B3", "B4"):
        live_state_paths[condition] = {path for row in b_stage_batches
            if row["entries"][0]["condition"] == condition
            and not any("/heldout/" in path for path in row["state_paths"])
            for path in row["state_paths"]}
        assert len(live_state_paths[condition]) == 1  # one SQLite/Application across the group's T1-T3 stages
    assert all(live_state_paths[left].isdisjoint(live_state_paths[right])
        for left, right in (("B2", "B3"), ("B2", "B4"), ("B3", "B4")))
    initial_equivalence = runner.journal.data["actions"]["formal:initial_state_equivalence"]["output"]
    assert set(initial_equivalence["fingerprints"]) == {"B2", "B3", "B4"}
    assert len(set(initial_equivalence["fingerprints"].values())) == 1
    assert len({str(runner.work / "states" / ("initial_" + condition))
                for condition in ("B2", "B3", "B4")}) == 3

    call_count = len(backend.calls)
    summary2 = asyncio.run(FormalRunner(root, "offline_formal", backend, gate_provider=offline_gate_provider,
        grader=grader, require_ready=False).run())
    assert summary2["recorded"] == 54 and len(backend.calls) == call_count
    changed_resume = FormalRunner(root, "offline_formal", backend, gate_provider=offline_gate_provider,
        grader=grader, batch_metadata={"changed": True}, require_ready=False)
    with pytest.raises(ValueError, match="resume fingerprint changed"):
        asyncio.run(changed_resume.run())


def test_offline_formal_resume_recovers_accepted_task_without_redispatch(tmp_path, monkeypatch):
    import pytest
    root = copy_benchmark(tmp_path)
    import runner.chain as chain_module
    import runner.formal as formal_module
    import runner.stage_packages as package_module

    def materialize_fixture(_root, manifest, destination):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "offline_fixture.py").write_text(manifest["repository_id"] + ":" + manifest["stage"])
        return {"offline_fixture": True}

    def collect_fixture(_workspace, _manifest, destination, **_kwargs):
        return {"inventory": [], "inventory_sha256": "offline", "patch_sha256": "offline",
            "artifact_dir": str(destination), "complete_patch": str(destination / "complete.patch")}

    monkeypatch.setattr(formal_module, "validate_stage_package", lambda *_args, **_kwargs: {"offline_fixture": True})
    monkeypatch.setattr(chain_module, "materialize_stage_package", materialize_fixture)
    monkeypatch.setattr(package_module, "materialize_stage_package", materialize_fixture)
    monkeypatch.setattr(chain_module, "collect_artifact", collect_fixture)

    backend = OfflineFormalBackend()
    backend.fail_once_after_accepting = "collector_repo_api_seed"
    grader = offline_grader(root, {}, {}, {})
    runner = FormalRunner(root, "offline_resume", backend, gate_provider=offline_gate_provider,
        grader=grader, require_ready=False)
    with pytest.raises(RuntimeError, match="simulated runner interruption"):
        asyncio.run(runner.run())
    accepted_after_failure = dict(backend.accepted_tasks)
    dispatches_after_failure = backend.task_dispatch_count
    assert len(accepted_after_failure) == 3
    assert dispatches_after_failure == 3

    recovered = FormalRunner(root, "offline_resume", backend, gate_provider=offline_gate_provider,
        grader=grader, require_ready=False)
    summary = asyncio.run(recovered.run())
    assert summary["recorded"] == 54
    assert backend.task_dispatch_count == 63  # 9 collector + 54 main accepted attempts, each once.
    assert len(backend.accepted_tasks) == 63
    assert backend.task_dispatch_count - dispatches_after_failure == 60
    assert any(row[0] == "task_recovered" and row[4] == "offline_collector_repo_api_seed"
               for row in backend.calls)


def test_live_formal_runner_fails_closed_without_nsrr_candidate_api(tmp_path):
    import pytest
    root = copy_benchmark(tmp_path)
    backend = OfflineFormalBackend()
    # Offline tests can inspect the runner, but a live mode cannot proceed with
    # only synthetic labels and no independent actual Gate-decision records.
    runner = FormalRunner(root, "blocked_live", backend, gate_provider=offline_gate_provider,
        grader=offline_grader(root, {}, {}, {}), require_ready=True)
    with pytest.raises(ValueError, match="direct evaluator for frozen NSRR candidates"):
        runner._ensure_live_ready()


def test_formal_cli_requires_explicit_positive_budget_before_any_execution():
    from scripts.run_formal import main
    with pytest.raises(SystemExit) as exc:
        main(["--execute"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["--execute", "--config", str(ROOT / "missing.yaml"), "--max-total-tokens", "0"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["--preflight-only"])
    assert exc.value.code == 2


def test_formal_cli_exit_code_tracks_runner_terminal_state(monkeypatch, capsys):
    import scripts.run_formal as cli
    import runner.chain as chain_module

    class FakeEvaluator:
        def bind_backend(self, _backend):
            pass

    async def ready_preflight(*_args, **_kwargs):
        return ({"ready": True, "checks": {}, "errors": []}, object(), FakeEvaluator())

    monkeypatch.setattr(cli, "preflight", ready_preflight)
    monkeypatch.setattr(chain_module, "LiveBackend", lambda *_args, **_kwargs: object())

    for complete, expected_code in ((True, 0), (False, 1)):
        class FakeRunner:
            def __init__(self, *_args, **_kwargs):
                pass

            async def run(self):
                return {"complete": complete, "missing_count": 0 if complete else 1}

        monkeypatch.setattr(cli, "FormalRunner", FakeRunner)
        code = cli.main(["--execute", "--config", "/unused/config.yaml", "--batch-id", "offline_cli",
            "--max-total-tokens", "1"])
        assert code == expected_code
        capsys.readouterr()


def test_readonly_formal_preflight_reports_direct_candidate_api_without_inference():
    from scripts.run_formal import preflight
    report, config, evaluator = asyncio.run(preflight(ROOT, ROOT.parent,
        None, None, check_services=False))
    assert config is None and evaluator is not None
    assert not report["ready"] and report["static_preflight_ready"]
    assert report["checks"]["planned_main_positions"] == 54
    assert report["checks"]["planned_a_positions"] == 18
    assert report["checks"]["planned_b_positions"] == 36
    assert report["checks"]["nsrr_freeze_verified"]
    assert report["checks"]["nsrr_candidate_evaluation_api_supported"] is True
    assert report["checks"]["nsrr_fixture_qualified"] is True
    assert report["checks"]["nsrr_gate_decision"] == "not_run"
    assert report["checks"]["actual_model_inference_requests"] == 0
    assert report["checks"]["embedding_requests"] == 0
    assert report["checks"]["rerank_requests"] == 0
    assert report["checks"]["external_inference_requests"] == 0


def test_compute_formal_metrics_and_unverified_exclusion(tmp_path):
    from runner.formal import compute_formal_metrics, render_formal_markdown_report
    rows = [
        {
            "position": 1,
            "condition": "A1",
            "repository_id": "repo_api",
            "stage": "t2_transfer",
            "stage_name": "transfer",
            "attempt_state": "terminal",
            "grade_status": "graded",
            "solved": True,
            "result": {
                "telemetry": {"tool_calls": 4, "reported_tokens": 1200},
                "grade": {"suites": {"regression": {"passed": True}}},
            },
        },
        {
            "position": 2,
            "condition": "A2",
            "repository_id": "repo_api",
            "stage": "t2_transfer",
            "stage_name": "transfer",
            "attempt_state": "terminal",
            "grade_status": "graded",
            "solved": True,
            "result": {
                "telemetry": {"tool_calls": 2, "reported_tokens": 800},
                "grade": {"suites": {"regression": {"passed": True}}},
            },
        },
        {
            "position": 3,
            "condition": "B2",
            "repository_id": "repo_api",
            "stage": "t1_seed",
            "stage_name": "seed",
            "attempt_state": "terminal",
            "grade_status": "unverified",
            "solved": None,
            "result": {
                "telemetry": {"tool_calls": 0, "reported_tokens": 0},
                "grade": {"status": "unverified", "solved": None, "reason": "model_service_or_transport_error"},
            },
        },
        {
            "position": 4,
            "condition": "B2",
            "repository_id": "repo_api",
            "stage": "t2_transfer",
            "stage_name": "transfer",
            "attempt_state": "terminal",
            "grade_status": "graded",
            "solved": False,
            "result": {
                "telemetry": {"tool_calls": 6, "reported_tokens": 2000},
                "grade": {"suites": {"regression": {"passed": False}}},
            },
        },
        {
            "position": 5,
            "condition": "B3",
            "repository_id": "repo_api",
            "stage": "t2_transfer",
            "stage_name": "transfer",
            "attempt_state": "terminal",
            "grade_status": "graded",
            "solved": True,
            "result": {
                "telemetry": {"tool_calls": 3, "reported_tokens": 1000},
                "grade": {"suites": {"regression": {"passed": True}}},
            },
        },
        {
            "position": 6,
            "condition": "B4",
            "repository_id": "repo_api",
            "stage": "t2_transfer",
            "stage_name": "transfer",
            "attempt_state": "terminal",
            "grade_status": "graded",
            "solved": True,
            "result": {
                "telemetry": {"tool_calls": 2, "reported_tokens": 700},
                "grade": {"suites": {"regression": {"passed": True}}},
            },
        },
    ]
    actions = {
        "B3:seed:task:repo_api:prepare": {
            "output": {
                "candidate_version_id": "cand-1",
                "run": {"payload": {"usage": {"total_tokens": 100}}},
            }
        },
        "B3:seed:task:repo_api:evaluate": {
            "output": {
                "candidate_version_id": "cand-1",
                "status": "evaluated",
                "run": {
                    "payload": {
                        "usage": {"total_tokens": 100},
                        "evaluation": {
                            "baseline": {"tokens": 80, "tool_calls": 1},
                            "candidate": {"tokens": 120, "tool_calls": 1},
                        },
                    }
                },
            }
        },
        "B3:seed:task:repo_api:publish": {
            "output": {
                "candidate_version_id": "cand-1",
                "status": "published",
                "run": {
                    "payload": {
                        "usage": {"total_tokens": 100},
                        "evaluation": {
                            "baseline": {"tokens": 80, "tool_calls": 1},
                            "candidate": {"tokens": 120, "tool_calls": 1},
                        },
                    }
                },
            }
        },
    }
    nsrr = {"status": "verified", "value": 0.0, "numerator": 0, "denominator": 6, "unverified_reason": None}
    metrics = compute_formal_metrics(rows, actions, ["repo_api"], nsrr)
    assert metrics["by_condition"]["B2"]["planned"] == 2
    assert metrics["by_condition"]["B2"]["verified"] == 1
    assert metrics["by_condition"]["B2"]["unverified"] == 1
    assert metrics["by_condition"]["B2"]["solve_rate"] == 0.0
    assert metrics["by_condition"]["B2"]["regression_rate"] == 1.0
    assert metrics["by_condition"]["B3"]["solve_rate"] == 1.0
    assert metrics["by_condition"]["B3"]["learning"]["total_learning_tokens"] == 300
    assert metrics["by_condition"]["B3"]["total_cost"]["total_tokens_per_solved"] == 1300.0
    assert metrics["paired_solved_cost"]["A1_vs_A2"]["common_solved_count"] == 1
    assert metrics["paired_solved_cost"]["A1_vs_A2"]["delta_online_tokens"] == -400.0
    assert metrics["skill_transfer"]["by_condition"]["B3"]["t2_transfer"]["solve_rate"] == 1.0
    report_lines = render_formal_markdown_report(
        {
            "batch_id": "b1",
            "formal_results": True,
            "complete": False,
            "planned": 6,
            "recorded": 6,
            "verified": 5,
            "unverified": 1,
            "missing_count": 0,
            "coverage": 5 / 6,
            "solved": 4,
            "solve_rate": 4 / 5,
            "successes_over_planned": 4 / 6,
            "nsrr": nsrr,
        },
        rows,
        metrics,
        nsrr,
        None,
    )
    report_md = "\n".join(report_lines)
    assert "## 1. Condition Summary" in report_md
    assert "## 4. Paired-Solved Cost" in report_md

