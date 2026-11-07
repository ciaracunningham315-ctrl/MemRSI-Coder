"""Offline orchestration contracts. No provider, HTTP server or model is used."""
import asyncio
from contextlib import asynccontextmanager
import copy
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.chain import (ChainRunner, Journal, LiveBackend, atomic_json, collect_artifact,
                          digest, execution_schedule, grade_main, read_json, restore_artifact)
from runner.dataset_validation import load_manifest, STAGES, inventory
from runner.stage_packages import materialize_stage_package


class OfflineBackend:
    offline = True

    def __init__(self):
        self.calls = []
        self.collections = {}
        self.crash_after_task = False
        self.crashed = False

    def budget(self):
        return {"stop_reason": None, "remaining_admissible_tokens": 999, "request_count": 0}

    def read(self, state):
        return read_json(state / "state.json")

    def write(self, state, data):
        atomic_json(state / "state.json", data)

    async def initialize(self, state, collection):
        self.write(state, {"tasks": [], "indexed": [], "skills": [], "meta": "V1"})
        self.collections[collection] = []
        return {"collection": collection, "history": {"task_ids": [], "event_ids": []}}

    def clone(self, source, destination):
        if not destination.exists():
            shutil.copytree(source, destination)
        self.calls.append(("clone", str(source), str(destination)))
        return {"source_sha256": digest(self.read(source))}

    async def audit(self, state, descriptor):
        indexed = self.read(state)["indexed"]
        assert self.collections[descriptor["collection"]] == indexed
        content = digest(indexed)
        assert descriptor.get("content_sha256", content) == content
        return {**descriptor, "content_sha256": content}

    async def task(self, state, condition, history, collection, workspace, prompt, manifest, save_identity):
        data = self.read(state)
        task_id = "offline_" + workspace.name
        if task_id not in data["tasks"]:
            self.calls.append(("task", condition, manifest["stage"], copy.deepcopy(data), copy.deepcopy(history)))
            assert set(history["task_ids"]) <= set(data["tasks"])
            assert history["event_ids"] == data["indexed"]
            data["tasks"].append(task_id)
            self.write(state, data)
        save_identity(task_id)
        if self.crash_after_task and not self.crashed:
            self.crashed = True
            raise RuntimeError("offline injected crash after durable Agent acceptance")
        (workspace / "new_binary.bin").write_bytes(b"\x00\xffoffline")
        return {"task_id": task_id, "status": "completed", "termination": "completed",
                "events": [{"event_id": task_id + "_event", "task_id": task_id, "event_type": "task.completed"}],
                "telemetry": {"model_calls": 0, "reported_tokens": None, "tokens_complete": False}}

    async def boundary(self, state, condition, history, old, new):
        data = self.read(state)
        assert data["indexed"] == self.collections[old]
        assert set(data["indexed"]) < set(history["event_ids"])
        data["indexed"] = list(history["event_ids"])
        self.write(state, data)
        self.collections[new] = list(data["indexed"])
        self.calls.append(("index", condition, copy.deepcopy(history)))
        return await self.audit(state, {"collection": new, "history": history})

    async def learn(self, state, condition, history, descriptor, ids, kind, key, stage, specs):
        self.calls.append(("learn", condition, stage, kind, list(ids), [row["task_id"] for row in specs]))
        assert ids == self.read(state)["tasks"]
        data = self.read(state)
        if kind == "task":
            data["skills"].append(f"{condition}_{stage}_{data['meta']}")
        else:
            assert condition == "B4" and stage == STAGES[1] and len(ids) == 2
            data["meta"] = "V2"
        self.write(state, data)
        return {"status": "offline_contract_only"}

    async def history_evidence(self, state, ids, workspace):
        return {"status": "offline_contract_only", "synthetic_evidence_in_real_reports": False}


def clone_fixture(tmp_path):
    root = tmp_path / "benchmark"
    root.mkdir()
    for name in ("scenarios", "private", "git_stage_packages"):
        shutil.copytree(ROOT / name, root / name)
    return root


def gate_specs(root, stage, kind, runtime):
    return [{"task_id": f"independent_{stage}_{kind}", "repository_id": "repo_api"}]


async def offline_grader(root, manifest, artifact, output):
    # Deliberately no fabricated positive grade, even in a fake transport test.
    return {"status": "graded", "solved": False, "boundary": "offline contract fixture"}


def test_schedule_has_exact_matrix_and_stable_group_stage_order():
    schedule = execution_schedule()
    assert schedule == execution_schedule()
    assert len(schedule) == 18
    for group in ("A1", "A2"):
        assert [row["stage"] for row in schedule if row["condition"] == group] == list(STAGES[1:])
    for group in ("B2", "B3", "B4"):
        assert [row["stage"] for row in schedule if row["condition"] == group] == list(STAGES)


def test_offline_chain_isolates_history_skills_and_heldout(tmp_path):
    root = clone_fixture(tmp_path)
    backend = OfflineBackend()
    runner = ChainRunner(root, "offline_contract", backend, gate_provider=gate_specs, grader=offline_grader)
    result = asyncio.run(runner.run())
    assert result["complete"] and result["recorded"] == 18
    assert not (root / "results").exists() and not (root / "reports").exists()
    tasks = [row for row in backend.calls if row[0] == "task"]
    assert len(tasks) == 21  # three public collector runs plus 18 positions
    for stage in ("transfer", "stale", "heldout"):
        a = {row[1]: row for row in tasks if row[1] in ("A1", "A2") and row[2] == stage}
        assert a["A1"][3] == a["A2"][3]
        assert a["A1"][4] == a["A2"][4]
    first = [row for row in tasks if row[1].startswith("B") and row[2] == "seed"]
    assert all(row[3] == first[0][3] for row in first)
    learns = [row for row in backend.calls if row[0] == "learn"]
    assert len(learns) == 7
    for condition in ("B3", "B4"):
        task_learns = [row for row in learns if row[1] == condition and row[3] == "task"]
        assert [len(row[4]) for row in task_learns] == [1, 2, 3]
    assert not any(row[1] == "B2" for row in learns)
    b4_calls = [row for row in backend.calls if row[0] in ("task", "learn") and row[1] == "B4"]
    meta = next(index for index, row in enumerate(b4_calls) if row[0] == "learn" and row[3] == "meta")
    assert b4_calls[meta-1][0:4] == ("learn", "B4", STAGES[1], "task")
    b4_t4 = next(row for row in tasks if row[1] == "B4" and row[2] == "heldout")
    assert b4_t4[3]["meta"] == "V2"
    assert b4_t4[3]["skills"][-1].endswith("V2")
    assert len(b4_t4[3]["tasks"]) == 3
    assert len([row for row in backend.calls if row[0] == "index"]) == 12
    count = len(backend.calls)
    result2 = asyncio.run(ChainRunner(root, "offline_contract", backend, gate_provider=gate_specs, grader=offline_grader).run())
    assert result2["complete"] and len(backend.calls) == count


def test_crash_recovers_accepted_attempt_without_reexecution(tmp_path):
    root = clone_fixture(tmp_path)
    backend = OfflineBackend()
    backend.crash_after_task = True
    with pytest.raises(RuntimeError, match="injected crash"):
        asyncio.run(ChainRunner(root, "resume", backend, gate_provider=gate_specs, grader=offline_grader).run())
    runner = ChainRunner(root, "resume", backend, gate_provider=gate_specs, grader=offline_grader)
    result = asyncio.run(runner.run())
    assert result["complete"]
    assert len([row for row in backend.calls if row[0] == "task"]) == 21
    transitions = runner.journal.data["transitions"]
    assert any(row["values"].get("phase") == "agent_running" for row in transitions)
    assert all(row["phase"] == "recorded" for row in runner.journal.data["tasks"].values())


def test_binary_new_file_delete_and_mode_patch_replays_from_clean_snapshot(tmp_path):
    manifest = load_manifest(ROOT, STAGES[0])
    workspace = tmp_path / "agent"
    materialize_stage_package(ROOT, manifest, workspace)
    (workspace / "new.bin").write_bytes(bytes(range(256)))
    (workspace / "new.bin").chmod(0o755)
    removed = next(path for path in workspace.rglob("*.py") if ".git" not in path.parts)
    removed.unlink()
    # A modified Agent index and ignored additions must not lose the full diff.
    (workspace / ".gitignore").write_text("new.bin\n")
    # Collection must never execute an Agent-injected Git clean filter on host.
    marker = tmp_path / "unsafe_filter_executed"
    with (workspace / ".git/config").open("a") as config:
        config.write(f'\n[filter "unsafe"]\n\tclean = touch {marker}\n\trequired = false\n')
    (workspace / ".gitattributes").write_text("*.bin filter=unsafe\n")
    artifact = collect_artifact(workspace, manifest, tmp_path / "artifact")
    assert not marker.exists()
    target = tmp_path / "grader"
    restore_artifact(ROOT, manifest, artifact, target)
    assert inventory(workspace) == inventory(target)
    assert (target / "new.bin").read_bytes() == bytes(range(256))
    assert (target / "new.bin").stat().st_mode & 0o111
    assert "GIT binary patch" in Path(artifact["complete_patch"]).read_text()


def test_host_only_grader_is_explicit_offline_engineering_check(tmp_path):
    manifest = load_manifest(ROOT, STAGES[0])
    workspace = tmp_path / "agent"
    materialize_stage_package(ROOT, manifest, workspace)
    reference = ROOT / manifest["reference_dir"]
    for path in workspace.iterdir():
        if path.name != ".git":
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    shutil.copytree(reference, workspace, dirs_exist_ok=True)
    # Agent-owned tests are deleted: trusted grading comes from clean private suites.
    shutil.rmtree(workspace / "tests")
    artifact = collect_artifact(workspace, manifest, tmp_path / "artifact")
    result = asyncio.run(grade_main(ROOT, manifest, artifact, tmp_path / "private_grader", "unused", host_engineering_only=True))
    assert result["solved"] and result["isolation"] == "host_offline_test_only"
    assert all(row["tests_run"] > 0 for row in result["suites"].values())
    assert not result["private_feedback_exported_to_agent"]
    assert not (workspace / "_trusted_tests").exists()


def test_journal_rejects_changed_inputs_and_phase_rollback(tmp_path):
    journal = Journal(tmp_path)
    journal.update("tasks", "one", phase="artifact_saved")
    with pytest.raises(ValueError, match="backwards"):
        journal.update("tasks", "one", phase="agent_running")
    async def action(): return {"actual": "fixture"}
    asyncio.run(journal.once("action", {"trace_ids": ["one"]}, action))
    with pytest.raises(ValueError, match="inputs changed"):
        asyncio.run(journal.once("action", {"trace_ids": ["two"]}, action))


def test_ledger_environment_is_inherited_by_all_model_clients():
    source = (ROOT.parent / "src/evocode/llm.py").read_text()
    harness = (ROOT.parent / "scripts/blackbox_runtime.py").read_text()
    isolated = (ROOT.parent / "src/evocode/app.py").read_text()
    assert "self.token_budget = budget_from_environment()" in source
    assert 'env["EVOCODE_TOKEN_BUDGET_DB"]' in harness
    assert "else ModelClient(config)" in isolated


@pytest.mark.parametrize("passed", [True, False])
def test_live_http_evaluate_waits_for_terminal_report_before_publish(tmp_path, passed):
    """The real adapter is driven by a fake transport, never by a model."""
    backend = LiveBackend.__new__(LiveBackend)
    requests = []
    run = {"run_id": "actual-http-run", "kind": "task", "status": "candidate", "payload": {
        "idempotency_key": "stable-key", "trace_task_ids": ["actual-t1"],
        "candidate": {"version_id": "actual-candidate-version", "level": "project", "scope_id": "repo_api"}}}

    class Transport:
        async def request(self, method, path, json=None):
            requests.append((method, path))
            if path == "/api/evolution": return [copy.deepcopy(run)]
            if path.endswith("/evaluate"):
                assert json["frozen_memory_collection"] == "frozen-index"
                run["status"] = "evaluating"
                return {"scheduled": True}
            if path.endswith("/publish"):
                assert run["status"] == "evaluated" and run["payload"]["evaluation"]["passed"]
                run["status"] = "published"
                return copy.deepcopy(run)
            if path == "/api/evolution/actual-http-run": return copy.deepcopy(run)
            if path == "/api/skills/versions": return []
            if path == "/api/evolution/active-meta": return {"version_id": "builtin"}
            raise AssertionError(path)

        async def wait_idle(self):
            requests.append(("WAIT", "actual-job-terminal"))
            assert run["status"] == "evaluating"
            run["status"] = "evaluated" if passed else "needs_review"
            run["payload"]["evaluation"] = {"passed": passed}

    @asynccontextmanager
    async def instance(*args): yield Transport()
    async def audit(*args): return {"content_sha256": "same"}
    backend.instance, backend.audit = instance, audit
    backend.budget = lambda: {"stop_reason": None}
    result = asyncio.run(backend.learn(tmp_path, "B3", {"task_ids": ["actual-t1"], "event_ids": []},
        {"collection": "frozen-index"}, ["actual-t1"], "task", "stable-key", STAGES[0], [{"task_id": "independent"}]))
    assert result["run"]["status"] == ("published" if passed else "needs_review")
    publish = ("POST", "/api/evolution/actual-http-run/publish")
    assert (publish in requests) is passed
    if passed:
        assert requests.index(("WAIT", "actual-job-terminal")) < requests.index(publish)


def test_model_service_503_failure_marks_grade_unverified_and_preserves_solve_rate(tmp_path):
    """验证模型服务 HTTP 503 故障被归为 unverified，不计入有效分母也不拉低 Solve Rate。"""
    root = clone_fixture(tmp_path)
    backend = OfflineBackend()
    original_task = backend.task

    async def task_with_503(state, condition, history, collection, workspace, prompt, manifest, save_identity):
        output = await original_task(state, condition, history, collection, workspace, prompt, manifest, save_identity)
        if condition == "A2" and manifest["stage"] == "transfer":
            return {
                **output,
                "status": "failed",
                "termination": "failed",
                "state": {"error": "ModelError: planner HTTP 503: Service Unavailable"},
                "events": [{
                    "event_id": output["task_id"] + "_fail",
                    "task_id": output["task_id"],
                    "event_type": "task.failed",
                    "payload": {"diagnostic": {"error": "ModelError: planner HTTP 503: Service Unavailable"}},
                }],
                "telemetry": {"model_calls": 0, "reported_tokens": None, "tokens_complete": False},
            }
        return output

    backend.task = task_with_503
    graded_attempts = []

    async def positive_grader(root_dir, manifest, artifact, output_dir):
        graded_attempts.append((manifest["repository_id"], manifest["stage"]))
        return {"status": "graded", "solved": True, "boundary": "offline contract fixture"}

    runner = ChainRunner(root, "http503_unverified", backend, gate_provider=gate_specs, grader=positive_grader)
    summary = asyncio.run(runner.run())
    assert summary["planned"] == 18
    assert summary["recorded"] == 18
    assert summary["verified"] == 17
    assert summary["unverified"] == 1
    assert summary["solved"] == 17
    assert summary["solve_rate"] == 1.0
    a2_t2 = next(row for row in summary["results"] if row["condition"] == "A2" and row["stage"] == "transfer")
    assert a2_t2["grade"]["status"] == "unverified"
    assert a2_t2["grade"]["reason"] == "model_service_error"
    assert a2_t2["solved"] is None
