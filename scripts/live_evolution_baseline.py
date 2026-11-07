"""Bounded live DeepSeek and Docker Task Skill / Meta-Skill comparison.

The script creates synthetic repositories and a disposable SQLite store.
The cycle phase trains on a real execution and publishes only to that store.

Run from the repository root:

    .venv/bin/python scripts/live_evolution_baseline.py

Full-phase DeepSeek completions are capped at 22 and estimated/admitted tokens
at 85,000. Meta-only runs default to 16 completions and 60,000 tokens. The
default report path is new for each run under data/live-evolution-baselines.
Cycle runs are capped at 36 completions and 135,000 estimated/admitted tokens.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from evocode.app import Application
from evocode.config import load_config
from evocode.contracts import TaskSpec
from evocode.llm import ModelClient

DEFAULT_COMPLETION_LIMIT = 22
DEFAULT_TOKEN_LIMIT = 85_000
TRAIN_SOURCE = "def add(left, right):\n    return left - right\n"
TRAIN_TESTS = (
    "import unittest\n"
    "from calc import add\n\n"
    "class TestAdd(unittest.TestCase):\n"
    "    def test_positive(self):\n        self.assertEqual(add(7, 4), 11)\n\n"
    "    def test_negative(self):\n        self.assertEqual(add(-2, -3), -5)\n"
)
TRAIN_PROMPT = (
    "Fix the synthetic add bug in calc.py with the smallest safe change. "
    "Do not edit tests. Run python -m unittest discover -s tests."
)
VALIDATION_SOURCE = "def multiply(left, right):\n    return left + right\n"
VALIDATION_TESTS = (
    "import unittest\n"
    "from calc import multiply\n\n"
    "class TestMultiply(unittest.TestCase):\n"
    "    def test_positive(self):\n        self.assertEqual(multiply(7, 4), 28)\n\n"
    "    def test_negative(self):\n        self.assertEqual(multiply(-2, -3), 6)\n"
)
VALIDATION_PROMPT = (
    "Fix the Python arithmetic regression in calc.py: multiply uses the wrong operator. "
    "Do not edit tests. Run python -m unittest discover -s tests."
)
ACCEPTANCE_COMMAND = ["python", "-m", "unittest", "discover", "-s", "tests"]
CYCLE_PROMPTS = {
    "training": "Repair the arithmetic contract for add in calc.py. Read CONTRIBUTING.md, then change the subtraction to addition.",
    "validation": "Repair the arithmetic contract for multiply. It should return the product of both inputs.",
    "reuse": "Repair the arithmetic contract for divide in calc.py. Its result should be left // right.",
}
CYCLE_SUFFIX = (
    " Follow the arithmetic contract workflow. If the repository workflow is already present in a loaded Skill, "
    "use it directly. Otherwise read CONTRIBUTING.md before editing. Do not modify tests or documentation."
)
CYCLE_WORKFLOW = (
    "# Arithmetic contract workflow\n\n"
    "The live arithmetic implementation is calc.py at the repository root. "
    "Read that file directly, then make one exact edit to the requested function's return expression. "
    "Leave tests and documentation unchanged. The controller has an independent acceptance command. "
    "After the edit, return the CoderReport and let the controller run that check.\n"
)
REUSE_SOURCE = "def divide(left, right):\n    return left + right\n"
REUSE_TESTS = (
    "import unittest\nfrom calc import divide\n\n"
    "class TestDivide(unittest.TestCase):\n"
    "    def test_exact(self):\n        self.assertEqual(divide(12, 3), 4)\n"
    "    def test_floor(self):\n        self.assertEqual(divide(7, 2), 3)\n"
)


class CompletionBudget:
    def __init__(self, completion_limit: int, token_limit: int):
        self.completion_limit = completion_limit
        self.token_limit = token_limit
        self.records: list[dict] = []
        self.http_requests: list[dict] = []
        self.admitted_tokens = 0
        self.measured_tokens = 0
        self.measured_complete = True
        self.admission_denial = None

    def admit(self, messages, max_tokens: int | None, role: str) -> dict:
        serialized = json.dumps(messages, ensure_ascii=False).encode("utf-8")
        estimate = len(serialized) // 3 + (max_tokens or 2048) + 256
        if len(self.records) >= self.completion_limit:
            self.admission_denial = "completion_limit"
            raise RuntimeError("DeepSeek completion limit reached")
        if self.measured_complete:
            projected = self.measured_tokens + estimate
        else:
            projected = sum(item["estimated_tokens"] for item in self.records) + estimate
        if projected > self.token_limit:
            self.admission_denial = "estimated_token_limit"
            raise RuntimeError("DeepSeek estimated-token limit reached")
        self.admitted_tokens = max(self.admitted_tokens, projected)
        record = {
            "role": role,
            "status": "started",
            "estimated_tokens": estimate,
            "projected_tokens_with_next_call": projected,
        }
        self.records.append(record)
        return record


@contextmanager
def meter_http_attempts(budget: CompletionBudget):
    """Count physical chat-completion HTTP attempts, including client retries."""
    original_post = httpx.AsyncClient.post

    async def counted_post(client, url, *args, **kwargs):
        if str(url).rstrip("/").endswith("/chat/completions"):
            if len(budget.http_requests) >= budget.completion_limit:
                raise RuntimeError("DeepSeek HTTP attempt limit reached")
            record = {"path": "/chat/completions", "status": "started"}
            budget.http_requests.append(record)
            try:
                response = await original_post(client, url, *args, **kwargs)
            except BaseException as exc:
                record.update(status="failed", error_type=type(exc).__name__)
                raise
            record.update(status="response", status_code=response.status_code)
            return response
        return await original_post(client, url, *args, **kwargs)

    httpx.AsyncClient.post = counted_post
    try:
        yield
    finally:
        httpx.AsyncClient.post = original_post


class MeteredClient:
    """Shared per-completion and estimated-token gate for both model roles."""

    def __init__(self, client: ModelClient, budget: CompletionBudget):
        self.client = client
        self.budget = budget

    def for_config(self, config):
        return MeteredClient(self.client.for_config(config), self.budget)

    async def complete(self, messages, **kwargs):
        role = kwargs.get("role", "coder")
        models = self.client.config.models
        model = models.evolution if role == "evolution" else models.local
        record = self.budget.admit(messages, kwargs.get("max_tokens") or model.max_output_tokens, role)
        prefix = "Applicable versioned skills (subject to task constraints):\n"
        record["coder_skill_hashes"] = sorted({
            skill["content_sha256"] for message in messages
            if role == "coder" and isinstance(message.get("content"), str) and message["content"].startswith(prefix)
            for skill in json.loads(message["content"][len(prefix):])
            if skill.get("content_sha256")
        })
        started = time.monotonic()
        try:
            result = await self.client.complete(messages, **kwargs)
        except BaseException as exc:
            record.update(status="failed", error=type(exc).__name__)
            self.budget.measured_complete = False
            raise
        usage = result.usage or {}
        if "total_tokens" not in usage:
            self.budget.measured_complete = False
        else:
            self.budget.measured_tokens += usage["total_tokens"]
        record.update(
            status="completed",
            model=result.model,
            usage=usage,
            seconds=round(time.monotonic() - started, 3),
        )
        return result


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_fixture(root: Path, source: str, tests: str, workflow: str | None = None) -> dict[str, str]:
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(source, encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(tests, encoding="utf-8")
    if workflow:
        (root / "CONTRIBUTING.md").write_text(workflow, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=EvoCode fixture",
         "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Synthetic fixture"],
        check=True,
    )
    return {"tests/test_calc.py": sha256(root / "tests" / "test_calc.py")}


def seed_synthetic_trace(app: Application, fixture: Path, repository_id: str) -> str:
    task = app.store.create_task(
        TaskSpec(repo_path=str(fixture), prompt=TRAIN_PROMPT,
                 acceptance_command=ACCEPTANCE_COMMAND, repository_id=repository_id),
        app.config.snapshot(),
    )
    app.store.append_event(task["task_id"], "planner.plan", {
        "plan": {"goal": "Repair synthetic add arithmetic", "steps": [
            {"id": "inspect", "goal": "Inspect calc.py and test expectations"},
            {"id": "repair", "goal": "Replace subtraction with addition"},
            {"id": "verify", "goal": "Run the protected unittest acceptance"},
        ]},
    })
    app.store.append_event(task["task_id"], "tool.completed", {
        "tool": "read_file", "arguments": {"path": "calc.py"},
        "output": TRAIN_SOURCE,
    })
    app.store.append_event(task["task_id"], "test.result", {
        "command": ACCEPTANCE_COMMAND, "exit_code": 1,
        "output": "Synthetic acceptance failed: add(7, 4) returned 3, expected 11.",
    })
    app.store.append_event(task["task_id"], "tool.completed", {
        "tool": "edit_file", "arguments": {"path": "calc.py"},
        "output": "Synthetic correction changed subtraction to addition.",
    })
    app.store.update_task(task["task_id"], status="completed")
    return task["task_id"]


def compact_runner(result: dict) -> dict:
    task_passed = result.get("task_passed", result.get("passed")) is True
    tests_protected = result.get("tests_protected", result.get("acceptance_protected")) is True
    return {
        "status": result.get("status"),
        "task_passed": task_passed,
        "tests_protected": tests_protected,
        "tool_calls": result.get("tool_calls"),
        "tokens": result.get("tokens"),
        "duration_seconds": round(result.get("duration_seconds", 0), 3),
        "failure_reason": result.get("failure_reason") or result.get("error") or result.get("reason")
        or (result.get("status") if not task_passed else None),
        "unverified": result.get("unverified", False),
        "commit": result.get("commit"),
        "coder_skill_hashes": result.get("coder_skill_hashes", []),
    }


def compact_evaluation(run: dict) -> dict:
    payload = run.get("payload", {})
    report = payload.get("evaluation") or {}
    rows = []
    for row in report.get("rows", []):
        rows.append({
            "commit": row.get("spec", {}).get("commit"),
            "baseline": compact_runner(row.get("baseline", {})),
            "candidate": compact_runner(row.get("candidate", {})),
        })
    stages = []
    for stage in payload.get("meta_stages", []):
        stages.append({
            "branch": stage.get("branch"),
            "candidate_name": stage.get("candidate", {}).get("name"),
            "content_sha256": stage.get("candidate", {}).get("content_sha256"),
            "proposal_usage": stage.get("usage", {}),
        })
    service_error = payload.get("error")
    if not service_error:
        failure = None
    elif "input_budget" in service_error or "token limit" in service_error:
        failure = "budget_exceeded"
    elif "scope" in service_error.lower():
        failure = "invalid_skill_scope"
    elif "operation" in service_error.lower():
        failure = "invalid_skill_operation"
    elif "evidence" in service_error.lower():
        failure = "invalid_evidence_reference"
    elif "json" in service_error.lower():
        failure = "invalid_model_json"
    else:
        failure = "evaluation_error"
    return {
        "status": run.get("status"),
        "failure_code": failure,
        "passed": report.get("passed"),
        "reasons": report.get("reasons", []),
        "target_metric": report.get("target_metric"),
        "baseline": report.get("baseline"),
        "candidate": report.get("candidate"),
        "rows": rows,
        "meta_stages": stages,
    }


def validate_prior_task_comparison(source: dict, comparison: dict) -> None:
    """Reject incomplete or unsuccessful evidence before importing it as Meta prior."""
    if not isinstance(source, dict) or not isinstance(comparison, dict):
        raise ValueError("Prior report is missing a Task Skill comparison")
    rows = comparison.get("rows", [])
    if comparison.get("status") != "evaluated":
        raise ValueError("Prior Task Skill comparison was not evaluated")
    if comparison.get("passed") is not True or not isinstance(rows, list) or not rows:
        raise ValueError("Prior Task Skill evaluation did not pass")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"Prior row {index} is malformed")
        for side in ("baseline", "candidate"):
            result = row.get(side)
            if not isinstance(result, dict):
                raise ValueError(f"Prior row {index} {side} result is missing")
            protected = result.get("tests_protected", result.get("acceptance_protected"))
            passed = result.get("task_passed", result.get("passed"))
            if protected is not True:
                raise ValueError(f"Prior row {index} {side} acceptance tests were not protected")
            if passed is not True:
                raise ValueError(f"Prior row {index} {side} task acceptance failed")


def meta_evaluation_gates(comparison: dict) -> dict:
    """Summarize the evidence required before a Meta comparison can pass."""
    rows = comparison.get("rows") or []
    stages = {stage.get("branch"): stage.get("content_sha256")
              for stage in comparison.get("meta_stages", [])}
    generated_skills_reached_coder = bool(rows) and all(
        stages.get(branch) and stages[branch] in (row.get(branch) or {}).get("coder_skill_hashes", [])
        for row in rows for branch in ("baseline", "candidate")
    )
    branches_verified = bool(rows) and all(
        (row.get(branch) or {}).get("tests_protected") is True
        and (row.get(branch) or {}).get("task_passed") is True
        for row in rows for branch in ("baseline", "candidate")
    )
    reasons = []
    if not rows:
        reasons.append("no_evaluation_rows")
    for index, row in enumerate(rows):
        for branch in ("baseline", "candidate"):
            result = row.get(branch) or {}
            digest = stages.get(branch)
            if not digest or digest not in result.get("coder_skill_hashes", []):
                reasons.append(f"{branch}_generated_skill_not_injected_row_{index}")
            if result.get("tests_protected") is not True:
                reasons.append(f"{branch}_acceptance_tests_not_protected_row_{index}")
            if result.get("task_passed") is not True:
                detail = result.get("failure_reason") or result.get("status") or "acceptance_failed"
                reasons.append(f"{branch}_task_failed_row_{index}: {detail}")
    return {
        "both_generated_skills_reached_coder": generated_skills_reached_coder,
        "both_branches_verified": branches_verified,
        "passed": generated_skills_reached_coder and branches_verified,
        "failure_reasons": reasons,
    }


async def verify_learning_cycle(app, budget, report, training, validation, root, repository_id):
    reuse = root / "reuse-fixture"
    reuse_hashes = make_fixture(reuse, REUSE_SOURCE, REUSE_TESTS, CYCLE_WORKFLOW)

    def spec(workspace, stage):
        return TaskSpec(repo_path=str(workspace), repository_id=repository_id,
                        prompt=CYCLE_PROMPTS[stage] + CYCLE_SUFFIX,
                        acceptance_command=ACCEPTANCE_COMMAND)

    def require(check, reason):
        if not check:
            report["cycle_failure"] = reason
            raise RuntimeError(reason)

    training_test_hash = sha256(training / "tests/test_calc.py")
    training_source_hash = sha256(training / "calc.py")
    task = app.create_task(spec(training, "training"))
    trained = await app.run(task["task_id"])
    events = app.store.read_events(task_id=task["task_id"])
    patches = {event["event_id"] for event in events if event["event_type"] == "code.patch"}
    report["training"] = {"status": trained["status"], "task_id": task["task_id"],
                          "checks": trained["state"].get("verification", {}).get("status"),
                          "patch_event_ids": sorted(patches), "error": trained["state"].get("error")}
    require(trained["status"] == "completed" and report["training"]["checks"] == "passed"
            and sha256(training / "tests/test_calc.py") == training_test_hash
            and sha256(training / "calc.py") != training_source_hash
            and bool(patches), "training_delivery_or_acceptance_failed")

    run = await app.evolution.trigger(task["task_id"])
    candidate = run["payload"].get("candidate") or {}
    digest = candidate.get("content_sha256")
    refs = set((run["payload"].get("proposal") or {}).get("evidence_event_ids", []))
    report["cycle_proposal"] = {"status": run["status"], "version_id": candidate.get("version_id"),
                                "content_sha256": digest, "evidence_event_ids": sorted(refs),
                                "links_to_real_patch": bool(refs & patches),
                                "error": run["payload"].get("error"), "reason": run["payload"].get("reason")}
    require(run["status"] == "candidate" and bool(digest) and bool(refs)
            and refs <= {event["event_id"] for event in events}, "candidate_or_trace_reference_invalid")
    require(not app.skills.snapshot(repository_id), "candidate_active_before_evaluation")

    validation_hashes = {file: sha256(validation / file)
                         for file in ("calc.py", "tests/test_calc.py", "CONTRIBUTING.md")}
    eval_spec = spec(validation, "validation").model_dump()
    eval_spec["acceptance_files"] = ["tests/test_calc.py"]
    evaluated = await app.evolution.evaluate(run["run_id"], [eval_spec])
    comparison = compact_evaluation(evaluated)
    report["task_skill_comparison"] = comparison
    require(all(sha256(validation / path) == digest for path, digest in validation_hashes.items())
            and not app.skills.snapshot(repository_id), "evaluation_changed_source_or_active_versions")
    require(bool(comparison["rows"]) and all(
        row["baseline"]["acceptance_protected"] and row["candidate"]["acceptance_protected"]
        and digest in row["candidate"]["coder_skill_hashes"]
        and digest not in row["baseline"]["coder_skill_hashes"] for row in comparison["rows"]
    ), "invalid_or_unexercised_comparison")
    require(evaluated["status"] == "evaluated" and comparison["passed"], "candidate_did_not_pass_promotion_gate")

    published = app.evolution.publish(run["run_id"])["payload"]["published_version"]
    report["isolation"]["published"] = True
    require(published["version_id"] == candidate["version_id"]
            and app.skills.snapshot(repository_id).get(published["skill_id"]) == published["version_id"],
            "unexpected_published_version")
    next_task = app.create_task(spec(reuse, "reuse"))
    start = len(budget.records)
    result = await app.run(next_task["task_id"])
    events = app.store.read_events(task_id=next_task["task_id"])
    checks = {
        "completed": result["status"] == "completed",
        "acceptance_passed": result["state"].get("verification", {}).get("status") == "passed",
        "protected_tests_unchanged": sha256(reuse / "tests/test_calc.py") == reuse_hashes["tests/test_calc.py"],
        "version_recorded": any(event["event_type"] == "skill.loaded"
                               and event["payload"]["version_id"] == published["version_id"] for event in events),
        "content_sent_to_coder": any(digest in record["coder_skill_hashes"] and record["status"] == "completed"
                                    for record in budget.records[start:]),
    }
    report["reuse"] = {"task_id": next_task["task_id"], "version_id": published["version_id"],
                       "checks": checks, "error": result["state"].get("error")}
    require(all(checks.values()), "published_skill_reuse_failed")
    report["status"] = "completed"


def report_path(requested: str | None) -> Path:
    path = (Path(requested).expanduser().resolve() if requested else
            Path("data/live-evolution-baselines") /
            f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.json").resolve()
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def usage_report(budget):
    return {
        "unit": "logical completions and physical /chat/completions HTTP attempts",
        "completion_attempts": len(budget.records),
        "completed_completions": sum(r["status"] == "completed" for r in budget.records),
        "failed_or_interrupted_completions": sum(r["status"] == "failed" for r in budget.records),
        "measured_total_tokens": budget.measured_tokens,
        "all_usage_reported": budget.measured_complete,
        "admitted_estimated_tokens": budget.admitted_tokens,
        "admission_denial": budget.admission_denial,
        "http_request_attempts": len(budget.http_requests),
        "http_requests": budget.http_requests,
        "requests": budget.records,
    }


def import_task_history(app: Application, task_id: str, source_report: Path) -> dict:
    source = json.loads(source_report.read_text(encoding="utf-8"))
    comparison = source.get("task_skill_comparison")
    proposal = source.get("task_skill_proposal") or {}
    validate_prior_task_comparison(source, comparison or {})
    source_boundary = {
        "report_status": source.get("status"),
        "report_error_type": source.get("error_type"),
        "report_error": source.get("error"),
        "task_skill_comparison_status": comparison.get("status"),
        "task_skill_comparison_passed": comparison.get("passed"),
    }
    run_id = str(uuid.uuid4())
    now = datetime.now().astimezone().isoformat()
    payload = {
        "manual": True,
        "trigger": "manual",
        "proposal": {
            "operation": "CREATE",
            "name": proposal.get("candidate_name") or "prior-live-task-skill",
            "reason": "Prior isolated live Task Skill comparison; see linked report.",
        },
        "evaluation": comparison,
        "source_report": str(source_report.resolve()),
        "source_run_boundary": source_boundary,
    }
    with app.store.transaction() as conn:
        conn.execute(
            "INSERT INTO evolution_runs VALUES(?,?,?,?,?,?,?,?)",
            (run_id, "task", "needs_review", task_id, None,
             json.dumps(payload, ensure_ascii=False), now, now),
        )
    return {"run_id": run_id, "comparison": comparison, "source_report": str(source_report.resolve()),
            "source_boundary": source_boundary}


async def execute(output: Path, completion_limit: int, token_limit: int,
                  phase: str, prior_task_report: Path | None) -> dict:
    config = load_config()
    if (config.models.evolution.base_url.rstrip("/"), config.models.evolution.model_id) != (
        "https://api.deepseek.com/v1", "deepseek-flash",
    ):
        raise RuntimeError("This acceptance requires the official deepseek-flash endpoint")
    if not os.getenv(config.models.evolution.api_key_env):
        raise RuntimeError("The configured DeepSeek API key is missing from the environment/.env")

    # Every proposal and every paired execution branch uses the configured
    # DeepSeek model. Disable unrelated services and isolate all state.
    config.models.local = config.models.evolution.model_copy(deep=True)
    config.models.local.thinking = "disabled"
    config.models.evolution.thinking = "disabled"
    config.models.local.max_output_tokens = min(config.models.local.max_output_tokens, 2048)
    config.data_dir = ""
    config.evolution.enabled = True
    config.evolution.auto_trigger_enabled = False
    config.evolution.allow_remote_code_context = True
    config.evolution.target_metric = "tool_calls"
    config.evolution.timeout = min(config.evolution.timeout, 300)
    config.skills.enabled = True
    config.raw_rag.enabled = False
    config.raw_rag.index_when_disabled = False
    config.raw_rag.background_indexing = False
    config.runtime.execution_backend = "docker"
    config.runtime.max_turns = min(config.runtime.max_turns, 8)

    budget = CompletionBudget(completion_limit, token_limit)
    report = {
        "status": "running",
        "model": config.models.evolution.model_id,
        "provider": "configured DeepSeek endpoint",
        "fixture": "synthetic add training trace and held-out multiply validation defect; two protected unittest cases each",
        "evaluation_design": {
            "training_fixture": "synthetic add/subtract defect",
            "validation_fixture": "synthetic multiply/addition defect",
            "held_out_code_and_tests": True,
            "shared_synthetic_repository_scope": True,
        },
        "limits": {"completions": completion_limit, "estimated_tokens": token_limit},
        "isolation": {"temporary_sqlite": True, "temporary_fixture": True,
                      "docker_network": "none", "published": False},
    }
    with meter_http_attempts(budget), TemporaryDirectory(prefix="evocode-live-evolution-") as temp_dir:
        root = Path(temp_dir).resolve()
        training_fixture = root / "training-fixture"
        validation_fixture = root / "validation-fixture"
        training_fixture.mkdir()
        validation_fixture.mkdir()
        workflow = CYCLE_WORKFLOW if phase == "cycle" else None
        training_hashes = make_fixture(training_fixture, TRAIN_SOURCE, TRAIN_TESTS, workflow)
        protected_hashes = make_fixture(validation_fixture, VALIDATION_SOURCE, VALIDATION_TESTS, workflow)
        config.data_dir = str(root / "state")
        # Application.run_isolated constructs its own ModelClient. Route those
        # clients through the same gate as EvolutionService proposal calls.
        import evocode.app as app_module
        app_module.ModelClient = lambda branch_config: MeteredClient(ModelClient(branch_config), budget)
        app = Application(config, llm=MeteredClient(ModelClient(config), budget))
        run_isolated = app.evolution.evaluator.runner

        async def capture_branch_skills(spec, snapshot, workspace):
            start = len(budget.records)
            result = await run_isolated(spec, snapshot, workspace)
            result["coder_skill_hashes"] = sorted({
                digest for record in budget.records[start:] if record["status"] == "completed"
                for digest in record.get("coder_skill_hashes", [])
            })
            return result

        app.evolution.evaluator.runner = capture_branch_skills
        try:
            repo_id = "synthetic-fixture-repo"
            if phase == "cycle":
                report["fixture"] = "real add execution, held-out multiply evaluation, and divide reuse"
                report["evaluation_design"]["training_fixture"] = "real execution on a synthetic add defect"
                report["evaluation_design"]["workflow"] = "shared CONTRIBUTING.md, same task prompt in both branches"
                await verify_learning_cycle(app, budget, report, training_fixture, validation_fixture, root, repo_id)
                return report
            task_id = seed_synthetic_trace(app, training_fixture, repo_id)
            task_spec = {
                "repo_path": str(validation_fixture),
                "repository_id": repo_id,
                "prompt": VALIDATION_PROMPT,
                "acceptance_command": ACCEPTANCE_COMMAND,
                "acceptance_files": ["tests/test_calc.py"],
            }
            report["source"] = {"task_id": task_id, "repository_id": repo_id,
                                "training_test_sha256": training_hashes["tests/test_calc.py"],
                                "validation_test_sha256": protected_hashes["tests/test_calc.py"]}

            if phase == "full":
                task_run = await app.evolution.trigger(task_id, kind="task", manual=True)
                report["task_skill_proposal"] = {
                    "status": task_run["status"],
                    "candidate_name": (task_run.get("payload", {}).get("candidate") or {}).get("name"),
                    "usage": task_run.get("payload", {}).get("usage", {}),
                    "reason": task_run.get("payload", {}).get("reason"),
                }
                if task_run["status"] != "candidate":
                    raise RuntimeError("DeepSeek did not produce a valid Task Skill candidate")
                task_eval = await app.evolution.evaluate(task_run["run_id"], [task_spec])
                report["task_skill_comparison"] = compact_evaluation(task_eval)
                if task_eval["status"] == "failed" or any(
                    item["status"] == "failed" for item in budget.records
                ):
                    raise RuntimeError("Task Skill Docker comparison failed")
            else:
                if prior_task_report is None:
                    raise ValueError("Meta-only phase requires --prior-task-report")
                imported = import_task_history(app, task_id, prior_task_report)
                report["prior_task_comparison"] = compact_evaluation({
                    "status": "needs_review", "payload": {"evaluation": imported["comparison"]}
                })
                report["prior_task_report"] = imported["source_report"]
                report["prior_task_source_boundary"] = imported["source_boundary"]
                report["prior_task_comparison_independent_holdout"] = False

            meta_run = await app.evolution.trigger(task_id, kind="meta", manual=True)
            report["meta_skill_proposal"] = {
                "status": meta_run["status"],
                "candidate_name": (meta_run.get("payload", {}).get("candidate") or {}).get("name"),
                "usage": meta_run.get("payload", {}).get("usage", {}),
                "reason": meta_run.get("payload", {}).get("reason"),
                "error": meta_run.get("payload", {}).get("error"),
            }
            if meta_run["status"] != "candidate":
                raise RuntimeError("DeepSeek did not produce a valid Meta-Skill candidate")
            meta_eval = await app.evolution.evaluate(meta_run["run_id"], [task_spec])
            report["meta_skill_comparison"] = compact_evaluation(meta_eval)
            if any(item["status"] == "failed" for item in budget.records):
                raise RuntimeError("Meta-Skill Docker comparison failed")
            if meta_eval["status"] == "failed":
                report["status"] = "failed"
                report["error_type"] = "MetaEvaluationFailed"
                report["error"] = report["meta_skill_comparison"].get("failure_code")
                report["meta_skill_comparison"]["error"] = meta_eval.get("payload", {}).get("error")
            else:
                comparison = report["meta_skill_comparison"]
                comparison.update(meta_evaluation_gates(comparison))
                report["status"] = "completed" if comparison["passed"] else "failed"
                if report["status"] == "failed":
                    report["error_type"] = "MetaEvaluationGateFailed"
                    report["error"] = comparison["failure_reasons"][0]

            with app.store.transaction() as conn:
                active_versions = conn.execute(
                    "SELECT COUNT(*) FROM skill_versions WHERE status='active'"
                ).fetchone()[0]
            report["published_or_active_versions"] = active_versions
        except Exception as exc:
            report["status"] = "failed"
            report["error_type"] = type(exc).__name__
            report["error"] = "live_evolution_run_failed"
        finally:
            await app.close()
            report["deepseek_usage"] = usage_report(budget)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("full", "meta", "cycle"), default="full")
    parser.add_argument("--prior-task-report", type=Path)
    parser.add_argument("--max-completions", type=int)
    parser.add_argument("--max-estimated-tokens", type=int)
    parser.add_argument("--output")
    args = parser.parse_args()
    phase_call_limit = {"meta": 16, "cycle": 36}.get(args.phase, DEFAULT_COMPLETION_LIMIT)
    phase_token_limit = {"meta": 60_000, "cycle": 135_000}.get(args.phase, DEFAULT_TOKEN_LIMIT)
    args.max_completions = args.max_completions or phase_call_limit
    args.max_estimated_tokens = args.max_estimated_tokens or phase_token_limit
    if not 1 <= args.max_completions <= phase_call_limit:
        parser.error(f"--max-completions must be between 1 and {phase_call_limit} for phase {args.phase}")
    if not 1 <= args.max_estimated_tokens <= phase_token_limit:
        parser.error(f"--max-estimated-tokens must be between 1 and {phase_token_limit} for phase {args.phase}")
    if args.phase == "meta" and args.prior_task_report is None:
        parser.error("--prior-task-report is required for --phase meta")
    if args.phase != "meta" and args.prior_task_report is not None:
        parser.error("--prior-task-report is only valid for --phase meta")
    output = report_path(args.output)
    try:
        result = asyncio.run(execute(output, args.max_completions, args.max_estimated_tokens,
                                     args.phase, args.prior_task_report))
    except Exception as exc:
        result = {"status": "failed", "error_type": type(exc).__name__, "error": "preflight_failed"}
        result.setdefault("deepseek_usage", {"completion_attempts": 0})
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "report": str(output),
                      "deepseek_usage": result.get("deepseek_usage"),
                      "task_skill": result.get("task_skill_comparison"),
                      "meta_skill": result.get("meta_skill_comparison")},
                     ensure_ascii=False, indent=2))
    if result["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
