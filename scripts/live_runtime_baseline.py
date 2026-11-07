"""Budget-bounded live acceptance for the configured runtime model and Docker.

Run from the repository root with the same environment/config used by EvoCode:

    .venv/bin/python scripts/live_runtime_baseline.py

Each run creates a disposable workspace and SQLite store. Reports go to a new,
timestamped file under data/live-runtime-baselines unless --output is supplied.
The model must be local unless --allow-deepseek explicitly opts in to sending
only this script's synthetic inputs to the official DeepSeek API. No remote
evolution, embedding, or reranker service is used.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from evocode.app import Application
from evocode.config import load_config
from evocode.contracts import SessionMessageSpec
from evocode.llm import ModelClient

DEFAULT_REQUEST_LIMIT = 18
DEFAULT_TOKEN_LIMIT = 60_000
OUTPUT_TOKEN_LIMIT = 1_024
FIXTURE_SOURCE = "def add(a, b):\n    return a - b\n"
FIXTURE_TESTS = (
    "import unittest\n"
    "from calc import add\n\n"
    "class TestAdd(unittest.TestCase):\n"
    "    def test_positive(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
    "    def test_negative(self):\n        self.assertEqual(add(-1, -2), -3)\n"
)


class Budget:
    def __init__(self, request_limit: int, token_limit: int):
        self.request_limit = request_limit
        self.token_limit = token_limit
        self.requests: list[dict] = []
        self.estimated_tokens = 0
        self.total_tokens = 0
        self.actual_usage_complete = True


class FirstResponseGate:
    """Pause one model response before the controller can observe it."""

    def __init__(self):
        self.response_ready = asyncio.Event()
        self.release = asyncio.Event()
        self.claimed = False
        self.cancelled_before_handoff = False
        self.response_model = ""
        self.response_has_tools = False


class BudgetedLocalClient:
    """Meter all model requests while leaving ModelClient on the local role."""

    def __init__(self, client: ModelClient, budget: Budget, gate: FirstResponseGate | None = None):
        self.client = client
        self.budget = budget
        self.gate = gate

    def for_config(self, config):
        return BudgetedLocalClient(self.client.for_config(config), self.budget, self.gate)

    async def complete(self, messages, **kwargs):
        if kwargs.get("role") == "evolution":
            raise RuntimeError("Remote evolution calls are disabled in this live acceptance")
        prompt_bytes = len(json.dumps(
            {"messages": messages, "tools": kwargs.get("tools"),
             "response_format": kwargs.get("response_format")},
            ensure_ascii=False,
        ).encode("utf-8"))
        estimate = prompt_bytes // 3 + (kwargs.get("max_tokens") or OUTPUT_TOKEN_LIMIT) + 256
        if len(self.budget.requests) >= self.budget.request_limit:
            raise RuntimeError("Live acceptance request limit reached")
        has_completed_request = any(request["status"] == "completed" for request in self.budget.requests)
        if self.budget.actual_usage_complete and has_completed_request:
            admission_basis = "actual_plus_next_estimate"
            projected_tokens = self.budget.total_tokens + estimate
        else:
            admission_basis = "reserved_estimates"
            projected_tokens = self.budget.estimated_tokens + estimate
        if projected_tokens > self.budget.token_limit:
            raise RuntimeError("Live acceptance estimated-token limit reached")

        record = {
            "role": kwargs.get("role", "coder"),
            "status": "started",
            "estimated_tokens": estimate,
            "admission_basis": admission_basis,
            "projected_tokens_with_next_call": projected_tokens,
        }
        self.budget.requests.append(record)
        self.budget.estimated_tokens += estimate
        print(
            f"runtime request {len(self.budget.requests)}/{self.budget.request_limit}: {record['role']}",
            flush=True,
        )
        started = time.monotonic()
        try:
            result = await self.client.complete(messages, **kwargs)
        except BaseException as exc:
            record.update(status="failed", error=type(exc).__name__)
            self.budget.actual_usage_complete = False
            raise
        usage = result.usage.get("total_tokens", 0)
        if "total_tokens" not in result.usage:
            self.budget.actual_usage_complete = False
        self.budget.total_tokens += usage
        record.update(
            status="completed",
            model=result.model,
            usage=result.usage,
            seconds=round(time.monotonic() - started, 3),
        )
        if self.gate and not self.gate.claimed:
            self.gate.claimed = True
            self.gate.response_model = result.model
            self.gate.response_has_tools = bool(result.tool_calls)
            self.gate.response_ready.set()
            try:
                await self.gate.release.wait()
            except asyncio.CancelledError:
                self.gate.cancelled_before_handoff = True
                raise
        return result


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def event_evidence(events: list[dict], event_types: set[str]) -> list[dict]:
    evidence = []
    for event in events:
        event_type = event["event_type"]
        if event_type not in event_types:
            continue
        payload = event["payload"]
        if event_type == "planner.decision":
            decision = payload.get("decision") or {}
            summary = {
                "action": decision.get("action"),
                "check_ids": decision.get("check_ids", []),
                "assignment_step_id": (decision.get("assignment") or {}).get("step_id"),
                "reason": str(payload.get("reason") or "")[:240],
            }
        elif event_type == "node.transition":
            summary = {key: payload.get(key) for key in ("from", "to", "reason") if key in payload}
        elif event_type == "planner.tool.rejected":
            summary = {key: payload.get(key) for key in ("tool", "reason") if key in payload}
        elif event_type == "planner.decision_rejected":
            errors = payload.get("errors") or []
            if not isinstance(errors, list):
                errors = [errors]
            summary = {
                "attempt": payload.get("attempt"),
                "context_id": payload.get("context_id"),
                "errors": [str(error)[:240] for error in errors[:4]],
            }
        else:
            summary = payload
        evidence.append({"seq": event["seq"], "event_type": event_type, "payload": summary})
    return evidence


def require(checks: dict[str, bool], scenario: str) -> None:
    failures = [name for name, passed in checks.items() if not passed]
    if failures:
        raise RuntimeError(f"{scenario} failed checks: {', '.join(failures)}")


def require_local_endpoint(base_url: str) -> None:
    hostname = urlparse(base_url).hostname
    if not hostname:
        raise ValueError("The configured local model URL must include a hostname")
    if hostname.lower() == "localhost":
        return
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise ValueError("Refusing a non-local model hostname for this no-remote acceptance") from exc
    if not (address.is_private or address.is_loopback):
        raise ValueError("Refusing a public model endpoint for this no-remote acceptance")


def report_path(requested: str | None) -> Path:
    if requested:
        path = Path(requested).expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = Path("data/live-runtime-baselines") / f"{stamp}-{uuid.uuid4().hex[:8]}.json"
        path = path.resolve()
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def run(
    output: Path, request_limit: int, token_limit: int, scenario: str = "all", *, allow_deepseek: bool = False,
) -> dict:
    config = load_config()
    if not config.models.local.model_id:
        raise RuntimeError("No runtime model_id is configured")
    if allow_deepseek:
        if (config.models.local.base_url.rstrip("/"), config.models.local.model_id) != (
            "https://api.deepseek.com/v1", "deepseek-flash",
        ):
            raise ValueError("--allow-deepseek requires the official deepseek-flash runtime endpoint")
    else:
        require_local_endpoint(config.models.local.base_url)

    # Always use a synthetic workspace and isolate historical stores and other providers.
    with TemporaryDirectory(prefix="evocode-live-baseline-") as temporary:
        run_root = Path(temporary).resolve()
        workspace = run_root / "workspace"
        store_dir = run_root / "state"
        (workspace / "tests").mkdir(parents=True)
        source = FIXTURE_SOURCE.replace("a - b", "a + b") if scenario == "creation" else FIXTURE_SOURCE
        (workspace / "calc.py").write_text(source, encoding="utf-8")
        (workspace / "tests" / "test_calc.py").write_text(FIXTURE_TESTS, encoding="utf-8")
        config.data_dir = str(store_dir)
        config.models.local.max_output_tokens = OUTPUT_TOKEN_LIMIT
        config.runtime.execution_backend = "docker"
        config.runtime.max_turns = min(config.runtime.max_turns, 12)
        config.runtime.max_replans = min(config.runtime.max_replans, 2)
        config.runtime.planner_context_tokens = min(config.runtime.planner_context_tokens, 12_000)
        config.raw_rag.enabled = False
        config.raw_rag.index_when_disabled = False
        config.raw_rag.background_indexing = False
        config.raw_rag.rerank_enabled = False
        config.skills.enabled = False
        config.evolution.enabled = False
        config.evolution.auto_trigger_enabled = False

        budget = Budget(request_limit, token_limit)
        gate = FirstResponseGate() if scenario == "recovery" else None
        app = Application(config, BudgetedLocalClient(ModelClient(config), budget, gate))
        initial_hash = sha256(workspace / "calc.py")
        test_hash = sha256(workspace / "tests" / "test_calc.py")
        scenarios: list[dict] = []

        async def send(session_id: str, content: str):
            started = time.monotonic()
            first_request = len(budget.requests)
            receipt = await app.add_session_message(
                session_id,
                SessionMessageSpec(content=content, client_message_id=str(uuid.uuid4())),
            )
            result = await app.jobs[receipt["task_id"]]
            return result, case_metrics(first_request, started)

        def case_metrics(first_request: int, started: float) -> dict:
            requests = budget.requests[first_request:]
            return {
                "duration_seconds": round(time.monotonic() - started, 3),
                "model_requests": len(requests),
                "input_tokens": sum(r.get("usage", {}).get("prompt_tokens", 0) for r in requests),
                "output_tokens": sum(r.get("usage", {}).get("completion_tokens", 0) for r in requests),
                "reported_token_usage_complete": all(
                    "prompt_tokens" in r.get("usage", {}) and "completion_tokens" in r.get("usage", {})
                    for r in requests
                ),
            }

        def record(name: str, result: dict, checks: dict[str, bool], events: list[dict], metrics: dict, extra=None):
            item = {
                "name": name,
                "task_id": result["task_id"],
                "session_id": result["session_id"],
                "status": result["status"],
                "answer": result["state"].get("final_answer"),
                "error": result["state"].get("error"),
                "checks": checks,
                "metrics": {**metrics, "tool_calls": sum(e["event_type"] == "tool.started" for e in events)},
                "evidence": event_evidence(
                    events,
                    {
                        "user.message", "assistant.final", "context.assembled", "context.compacted", "requirements.updated",
                        "planner.decision", "planner.decision_rejected", "planner.tool.rejected", "node.transition",
                        "tool.started", "tool.completed", "tool.failed", "code.patch",
                        "coder.report.only_requested",
                        "verification.selected", "verification.result", "delivery.verification",
                    },
                ),
            }
            if extra:
                item.update(extra)
            scenarios.append(item)
            passed = all(checks.values())
            print(json.dumps({"scenario": name, "status": result["status"], "passed": passed,
                              "failed_checks": [key for key, value in checks.items() if not value]},
                             ensure_ascii=False), flush=True)
            require(checks, name)
            return item

        report = {
            "created_at": datetime.now(UTC).isoformat(),
            "status": "running",
            "model": config.models.local.model_id,
            "base_url": config.models.local.base_url,
            "execution_backend": config.runtime.execution_backend,
            "scenario_selection": scenario,
            "docker_image": config.runtime.docker_image,
            "sqlite_path": str(config.sqlite_path),
            "workspace": str(workspace),
            "request_limit": request_limit,
            "estimated_token_limit": token_limit,
            "output_token_limit": OUTPUT_TOKEN_LIMIT,
            "remote_evolution_embedding_reranker_calls": 0,
            "scenarios": scenarios,
        }

        recovery = None
        try:
            if scenario == "recovery":
                recovery_started = time.monotonic()
                session = app.create_session(str(workspace), "Live stop and resume acceptance")
                session_id = session["session"]["session_id"]
                stale_file = f"stale-{uuid.uuid4().hex[:10]}.txt"
                recovery = {
                    "name": "stop_correct_and_resume_same_task",
                    "status": "running",
                    "checks": {},
                    "evidence": {"stale_file": stale_file},
                }
                scenarios.append(recovery)
                initial = await app.add_session_message(
                    session_id,
                    SessionMessageSpec(
                        content=f"Create {stale_file} containing the word OLD and report STALE_WORK_DONE.",
                        client_message_id=str(uuid.uuid4()),
                    ),
                )
                task_id = initial["task_id"]
                recovery["task_id"] = task_id
                try:
                    await asyncio.wait_for(gate.response_ready.wait(), timeout=config.models.local.timeout + 5)
                except TimeoutError as exc:
                    recovery["status"] = "failed"
                    recovery["error"] = "Timed out waiting for the first real model response at the gate"
                    raise RuntimeError(recovery["error"]) from exc

                recovery["evidence"].update(
                    first_response_model=gate.response_model,
                    first_response_had_tool_calls=gate.response_has_tools,
                    first_response_held_before_controller=True,
                )
                stopped = await app.stop_task(task_id)
                recovery["evidence"]["stop_status"] = stopped["status"]
                exact_reply = f"RESUME_OK_{uuid.uuid4().hex[:12].upper()}"
                correction = await app.add_session_message(
                    session_id,
                    SessionMessageSpec(
                        content=(
                            f"Stop the previous work and discard that request. Do not create, edit, or inspect files. "
                            f"Reply with exactly this text and nothing else: {exact_reply}"
                        ),
                        intent="redirect",
                        client_message_id=str(uuid.uuid4()),
                    ),
                )
                resumed = await app.jobs[correction["task_id"]]
                events = app.store.read_events(task_id=task_id)
                user_events = [event for event in events if event["event_type"] == "user.message"]
                finals = [event for event in events if event["event_type"] == "assistant.final"]
                tool_events = [
                    event for event in events if event["event_type"] in {"tool.started", "tool.completed", "code.patch"}
                ]
                current_task = app.store.get_task(task_id)
                latest_user_seq = user_events[-1]["seq"] if user_events else None
                recovery["evidence"].update(
                    correction_task_id=correction["task_id"],
                    correction_event_id=correction["event_id"],
                    user_message_count=len(user_events),
                    latest_user_event_seq=latest_user_seq,
                    consumed_user_event_seq=resumed["state"].get("last_user_event_seq"),
                    final_count=len(finals),
                    final_answer=resumed["state"].get("final_answer"),
                    tool_event_types=[event["event_type"] for event in tool_events],
                    stale_file_exists=(workspace / stale_file).exists(),
                    gate_cancelled_before_handoff=gate.cancelled_before_handoff,
                    verification_events=event_evidence(
                        events,
                        {"user.message", "assistant.final", "node.transition", "planner.decision",
                         "planner.decision_rejected", "planner.tool.rejected", "context.assembled",
                         "tool.started", "tool.completed", "code.patch", "coder.report.only_requested"},
                    ),
                )
                recovery["status"] = resumed["status"]
                recovery["metrics"] = {**case_metrics(0, recovery_started), "tool_calls": len(tool_events)}
                recovery["answer"] = resumed["state"].get("final_answer")
                recovery["error"] = resumed["state"].get("error")
                recovery["checks"].update(
                    first_response_paused_before_controller=gate.response_ready.is_set(),
                    stop_interrupted_task=stopped["status"] == "interrupted",
                    correction_reused_same_task=correction["task_id"] == task_id == resumed["task_id"],
                    two_user_messages_same_task=len(user_events) == 2
                    and all(event["task_id"] == task_id for event in user_events),
                    latest_user_message_consumed=latest_user_seq is not None
                    and resumed["state"].get("last_user_event_seq") == latest_user_seq,
                    completed= resumed["status"] == "completed",
                    exact_corrected_reply=resumed["state"].get("final_answer") == exact_reply,
                    exactly_one_final=len(finals) == 1,
                    no_stale_answer=not any("STALE_WORK_DONE" in event["payload"].get("content", "") for event in finals),
                    no_stale_tool_or_file=not tool_events and not (workspace / stale_file).exists(),
                    stop_cancelled_gated_response=gate.cancelled_before_handoff,
                    task_state_matches_store=current_task["status"] == resumed["status"],
                )
                passed = all(recovery["checks"].values())
                print(json.dumps({"scenario": recovery["name"], "status": recovery["status"],
                                  "passed": passed,
                                  "failed_checks": [key for key, value in recovery["checks"].items() if not value]},
                                 ensure_ascii=False), flush=True)
                require(recovery["checks"], recovery["name"])
                recovery["status"] = "passed"

            if scenario == "all":
                # Ordinary conversation must complete through Planner without coder or tools.
                session = app.create_session(str(workspace), "Live runtime baseline")
                result, metrics = await send(session["session"]["session_id"], "2 + 3 等于多少？只输出数字，不要解释。")
                events = app.store.read_events(task_id=result["task_id"])
                finals = [event for event in events if event["event_type"] == "assistant.final"]
                record(
                    "direct_conversation",
                    result,
                    {
                        "completed": result["status"] == "completed",
                        "one_final_message": len(finals) == 1,
                        "answer_is_5": result["state"].get("final_answer", "").strip() == "5",
                        "planner_only": result["state"].get("coder_turns", 0) == 0,
                        "no_tool_execution": not any(event["event_type"] == "tool.started" for event in events),
                    },
                    events,
                    metrics,
                )

                # No fixed acceptance command: require automatic project-check discovery and execution.
                session = app.create_session(str(workspace), "File delivery and project check")
                result, metrics = await send(
                    session["session"]["session_id"],
                    "请检查 calc.py 和 tests/test_calc.py。修复 add(a, b) 使其返回两数之和，"
                    "只能修改 calc.py，运行项目已有测试，并简短说明实际完成和测试结果。",
                )
                events = app.store.read_events(task_id=result["task_id"])
                source = workspace / "calc.py"
                test_file = workspace / "tests" / "test_calc.py"
                delivered_hash = sha256(source) if source.is_file() else None
                observed_test_hash = sha256(test_file) if test_file.is_file() else None
                write_calls = {
                    event["payload"].get("tool_call_id")
                    for event in events
                    if event["event_type"] == "tool.started"
                    and event["payload"].get("tool") in {"edit_file", "run_command", "run_tests"}
                }
                written_paths = {
                    path
                    for event in events
                    if event["event_type"] == "code.patch"
                    or (event["event_type"] == "tool.completed" and event["payload"].get("tool_call_id") in write_calls)
                    for path in event["payload"].get("file_paths", [])
                }
                verification_results = [
                    event["payload"] for event in events if event["event_type"] == "verification.result"
                ]
                automatic_checks = [
                    event["payload"] for event in events
                    if event["event_type"] == "verification.selected" and event["payload"].get("source") == "automatic"
                ]
                run_tests_started = [
                    event for event in events
                    if event["event_type"] == "tool.started" and event["payload"].get("tool") == "run_tests"
                ]
                automatic_passed_checks = [
                    check
                    for item in verification_results
                    if item.get("status") == "passed" and item.get("source") == "automatic"
                    and item.get("coverage") == "project_checks"
                    for check in item.get("checks", [])
                    if check.get("status") == "passed" and check.get("check", {}).get("kind") == "test"
                ]
                passing_check_tool_ids = {
                    check.get("result", {}).get("tool_call_id") for check in automatic_passed_checks
                }
                project_check_started_ids = {
                    event["payload"].get("tool_call_id") for event in run_tests_started
                }
                project_check_tool_ids = passing_check_tool_ids & project_check_started_ids
                project_check_tool_completed = any(
                    event["event_type"] == "tool.completed"
                    and event["payload"].get("tool_call_id") in project_check_tool_ids
                    and event["payload"].get("status") == "completed"
                    and event["payload"].get("exit_code") == 0
                    for event in events
                )
                verification_passed = bool(automatic_passed_checks) and project_check_tool_completed
                record(
                    "file_delivery_and_project_check",
                    result,
                    {
                        "completed": result["status"] == "completed",
                        "one_final_message": sum(event["event_type"] == "assistant.final" for event in events) == 1,
                        "real_file_exists": source.is_file(),
                        "file_changed_from_fixture": delivered_hash is not None and delivered_hash != initial_hash,
                        "file_write_event_mentions_calc": any(path.endswith("calc.py") for path in written_paths),
                        "tests_unchanged": observed_test_hash == test_hash,
                        "automatic_check_selected": bool(automatic_checks)
                        and any(payload.get("checks") for payload in automatic_checks),
                        "project_test_tool_started": bool(run_tests_started),
                        "project_check_completion_event_passed": project_check_tool_completed,
                        "independent_project_check_passed": verification_passed,
                        "project_check_coverage_and_test_kind": bool(automatic_passed_checks),
                        "both_fixture_tests_executed": any(
                            "Ran 2 tests" in check.get("result", {}).get("output", "")
                            for check in automatic_passed_checks
                        ),
                        "delivery_verification_passed": any(
                            event["payload"].get("status") == "passed"
                            and any(path.endswith("calc.py") for path in event["payload"].get("targets", []))
                            for event in events if event["event_type"] == "delivery.verification"
                        ),
                    },
                    events,
                    metrics,
                    {
                        "file_sha256_before": initial_hash,
                        "file_sha256_after": delivered_hash,
                        "test_sha256": observed_test_hash,
                        "written_paths": sorted(written_paths),
                        "verification_results": verification_results,
                    },
                )

            if scenario == "creation":
                session = app.create_session(str(workspace), "New nested file delivery")
                target = workspace / "notes" / "result.txt"
                expected = "EVOCODE_FILE_DELIVERED"
                existed = target.exists()
                result, metrics = await send(
                    session["session"]["session_id"],
                    f"Create a new file notes/result.txt containing exactly the text {expected} "
                    "on one line. Create its parent directory if necessary. Do not change existing files.",
                )
                events = app.store.read_events(task_id=result["task_id"])
                record(
                    "create_nested_file",
                    result,
                    {
                        "completed": result["status"] == "completed",
                        "one_final_message": sum(e["event_type"] == "assistant.final" for e in events) == 1,
                        "new_file_has_expected_content": not existed and target.is_file()
                        and target.read_text(encoding="utf-8").rstrip("\n") == expected,
                        "existing_files_unchanged": sha256(workspace / "calc.py") == initial_hash
                        and sha256(workspace / "tests" / "test_calc.py") == test_hash,
                        "delivery_verification_passed": any(
                            e["event_type"] == "delivery.verification" and e["payload"].get("status") == "passed"
                            and str(target.resolve()) in e["payload"].get("targets", [])
                            for e in events
                        ),
                    },
                    events,
                    metrics,
                )

            if scenario == "continuity":
                session = app.create_session(str(workspace), "Long conversation continuity")
                sid = session["session"]["session_id"]
                fact = "VIOLET-47-ANCHOR"
                app.store.append_session_event(sid, "user.message", {
                    "content": f"The synthetic project identifier for later questions is {fact}.",
                })
                for index in range(1, 15):
                    app.store.append_session_event(sid, "user.message", {
                        "content": f"Historical fixture note {index}. " + ("This note does not change the project identifier. " * 16),
                    })
                    app.store.append_session_event(sid, "assistant.final", {"content": "Fixture note recorded."})
                for index, marker in enumerate(("FIRST", "SECOND"), 1):
                    result, metrics = await send(sid, f"Return the original project identifier followed by /{marker}. "
                                                 "Use exactly that format. Do not inspect or write files.")
                    events = app.store.read_events(task_id=result["task_id"])
                    session_row = app.store.get_session(sid)["session"]
                    record(
                        f"long_conversation_turn_{index}",
                        result,
                        {
                            "completed": result["status"] == "completed",
                            "one_final_message": sum(e["event_type"] == "assistant.final" for e in events) == 1,
                            "history_and_latest_request_both_used": result["state"].get("final_answer", "").strip()
                            == f"{fact}/{marker}",
                            "summary_preserved_oldest_fact": fact in session_row["summary"],
                            "summary_cursor_advanced": session_row["summary_through_seq"] > 1,
                            "planner_only": result["state"].get("coder_turns", 0) == 0,
                            "no_workspace_tools": not any(e["event_type"] == "tool.started" for e in events),
                        },
                        events,
                        metrics,
                    )

            report["status"] = "passed"
        except BaseException as exc:
            report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            if recovery is not None and recovery.get("status") == "running":
                recovery.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            report.update(
                requests=budget.requests,
                request_count=len(budget.requests),
                actual_tokens=budget.total_tokens,
                estimated_tokens_reserved=budget.estimated_tokens,
                actual_usage_complete=budget.actual_usage_complete,
                budget_admission_basis=("actual_plus_next_estimate" if budget.actual_usage_complete and budget.requests
                                        else "reserved_estimates"),
                scenarios=scenarios,
            )
            await app.close()
            with output.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(
                json.dumps({"report": str(output), "status": report["status"],
                            "requests": len(budget.requests), "actual_tokens": budget.total_tokens},
                           ensure_ascii=False),
                flush=True,
            )
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="report path (must not already exist)")
    parser.add_argument("--scenario", choices=("all", "recovery", "creation", "continuity"), default="all",
                        help="select conversation and repair, stop/resume, nested file creation, or long conversation")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_REQUEST_LIMIT)
    parser.add_argument("--allow-deepseek", action="store_true",
                        help="allow billed DeepSeek requests using this synthetic fixture only")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_TOKEN_LIMIT,
                        help="estimated input plus output-token budget")
    args = parser.parse_args()
    if args.max_requests < 1 or args.max_tokens < 1:
        parser.error("budget limits must be positive")
    asyncio.run(run(report_path(args.output), args.max_requests, args.max_tokens, args.scenario,
                    allow_deepseek=args.allow_deepseek))


if __name__ == "__main__":
    main()
