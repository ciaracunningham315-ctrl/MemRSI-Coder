"""Process isolation and read-only observations for EvoCode black-box evaluations."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile

import httpx
import yaml

from evocode.config import AppConfig, load_config

ROOT = Path(__file__).resolve().parents[1]


def load_live_config() -> AppConfig:
    config = load_config()
    config.runtime.execution_backend = "docker"
    return config


def clone_state(source: Path, destination: Path) -> None:
    """Copy a quiescent state, rebasing the two tables that store file paths."""
    source, destination = source.resolve(), destination.resolve()
    if destination.is_relative_to(source):
        raise ValueError("State destination must be outside the source state")
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with closing(sqlite3.connect((source / "evocode.db").as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(destination / "evocode.db")) as copy:
                original.backup(copy)
                for directory, table, column in (
                    ("artifacts", "artifacts", "storage_path"),
                    ("skill_versions", "skill_versions", "file_path"),
                ):
                    old_root = source / directory
                    if old_root.is_dir():
                        shutil.copytree(old_root, destination / directory)
                    for row_id, old in copy.execute(f"SELECT rowid, {column} FROM {table}").fetchall():
                        relative = Path(old).resolve().relative_to(old_root)
                        new = destination / directory / relative
                        copy.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (str(new), row_id))
                copy.commit()
    except BaseException:
        shutil.rmtree(destination)
        raise


def task_telemetry(events: list[dict], messages_by_context=None) -> dict:
    usage = [event["payload"] for event in events if event["event_type"] == "model.usage"]
    observed_ids = {item["context_id"] for item in usage if item.get("context_id")}
    messages_by_context = messages_by_context or {}
    contexts = []
    retrievals = []
    tool_evidence = {}
    for event in events:
        if event["event_type"] == "tool.completed":
            payload = event["payload"]
            retrieval = payload.get("metadata", {}).get("memory_retrieval")
            if retrieval is not None:
                retrievals.append(retrieval)
                tool_evidence[payload.get("tool_call_id")] = retrieval.get("evidence_event_ids", [])
    for event_index, event in enumerate(events):
        payload = event["payload"]
        if event["event_type"] == "context.assembled":
            context = payload["node_context"]
            row = {key: context.get(key) for key in ("context_id", "evidence_refs", "skill_refs")}
            row["role"] = payload["role"]
            row["artifact_id"] = payload.get("artifact_id")
            row["event_id"] = event.get("event_id")
            row["event_index"] = event_index
            row["model_call_observed"] = context.get("context_id") in observed_ids
            row["selection_point"] = ("after_plan" if any(
                prior.get("event_type") in {"planner.plan", "planner.replanned"}
                for prior in events[:event_index]) else "before_plan")
            messages = messages_by_context.get(payload["context_id"], [])
            row["tool_evidence_refs"] = sorted({
                event_id for message in messages if message.get("role") == "tool"
                for event_id in tool_evidence.get(message.get("tool_call_id"), [])
                if event_id in (message.get("content") or "")
            })
            row["skill_versions"] = []
            prefix = "Applicable versioned skills (subject to task constraints):\n"
            for message in messages:
                content = message.get("content")
                if message.get("role") == "user" and isinstance(content, str) and content.startswith(prefix):
                    for skill in json.loads(content[len(prefix):]):
                        row["skill_versions"].append({
                            "skill_id": skill.get("skill_id"),
                            "version_id": skill.get("version_id"),
                            "name": skill.get("name"),
                            "content_sha256": hashlib.sha256(skill["content"].encode()).hexdigest()
                            if isinstance(skill.get("content"), str) else None,
                        })
            # Keep every assembled context as evidence of selection (including an
            # explicit empty selection), but attach injected versions only when we
            # fetched the exact artifact used for a completed model call.
            refs = row.get("skill_refs")
            row["injection_verified"] = (refs == [] or
                                         bool(refs and messages and row["model_call_observed"] and
                                              row["skill_versions"]))
            row["injection_status"] = ("verified_injected" if refs and row["injection_verified"]
                                       else "verified_empty" if refs == []
                                       else "unverified")
            contexts.append(row)
        if event["event_type"] == "memory.retrieved":
            retrievals.append(payload)
    totals = [item.get("provider_total_tokens") for item in usage]
    return {
        "model_calls": len(usage),
        "reported_tokens": sum(value for value in totals if isinstance(value, int)),
        "tokens_complete": bool(totals) and all(isinstance(value, int) for value in totals),
        "retrievals": retrievals,
        "evidence_contexts": [item for item in contexts if item["model_call_observed"] and
                              (item["evidence_refs"] or item["tool_evidence_refs"])],
        "skill_contexts": [item for item in contexts if item["role"] == "coder"],
        "context_observations": contexts,
        "tool_calls": sum(event["event_type"] == "tool.started" for event in events),
    }


TRACE_EVENT_TYPES = {
    "task.created", "task.started", "task.completed", "task.failed", "task.interrupted",
    "skill.loaded", "skill.selection", "context.assembled", "planner.plan", "planner.replanned",
    "planner.decision", "planner.decision_rejected", "plan.step.started", "plan.step.completed",
    "plan.step.needs_replan", "tool.started", "tool.completed", "tool.failed", "code.patch",
    "verification.selected", "verification.result", "evolution.trigger_checked",
}


def task_trace(events: list[dict]) -> dict:
    """Return event identifiers and safe decision metadata, never prompt/source text."""
    rows = []
    for index, event in enumerate(events):
        kind = event.get("event_type")
        if kind not in TRACE_EVENT_TYPES:
            continue
        payload = event.get("payload") or {}
        row = {key: event.get(key) for key in ("event_id", "seq", "sequence", "event_type") if key in event}
        row["event_index"] = index
        if kind == "skill.loaded":
            row.update({key: payload.get(key) for key in
                        ("skill_id", "version_id", "reason", "matches", "selection_phase") if key in payload})
        elif kind == "skill.selection":
            row.update({"phase": payload.get("phase"), "loaded_skill_ids": payload.get("loaded_skill_ids", []),
                        "limit": payload.get("limit"),
                        "candidates": [{key: item.get(key) for key in
                                        ("skill_id", "version_id", "selection_reason", "matched_triggers",
                                         "reason", "matches", "load_status", "loaded") if key in item}
                                       for item in payload.get("candidates", []) if isinstance(item, dict)]})
        elif kind == "context.assembled":
            context = payload.get("node_context") or {}
            row.update({"context_id": payload.get("context_id"), "role": payload.get("role"),
                        "artifact_id": payload.get("artifact_id"),
                        "skill_refs": context.get("skill_refs", []),
                        "evidence_refs": context.get("evidence_refs", [])})
        elif kind in {"planner.plan", "planner.replanned"}:
            plan = payload.get("plan") or {}
            row.update({"reason_present": bool(payload.get("reason")),
                        "plan_step_ids": [step.get("id") for step in plan.get("steps", [])
                                          if isinstance(step, dict)],
                        "plan_step_count": len(plan.get("steps", []))})
        elif kind == "plan.step.started":
            row.update({key: payload.get(key) for key in ("step_id", "attempt") if key in payload})
        elif kind in {"planner.decision", "planner.decision_rejected"}:
            decision = payload.get("decision") or {}
            row.update({"action": decision.get("action"), "decision_errors": payload.get("errors"),
                        "reason_present": bool(payload.get("reason"))})
        elif kind.startswith("tool."):
            row.update({key: payload.get(key) for key in ("tool_call_id", "tool_name", "tool", "status")
                        if key in payload})
            metadata = payload.get("metadata") or {}
            if metadata.get("patch_artifact_id"):
                row["patch_artifact_id"] = metadata["patch_artifact_id"]
        elif kind == "code.patch":
            row.update({key: payload.get(key) for key in ("artifact_id", "tool_call_id") if key in payload})
            row["files"] = [str(path) for path in payload.get("file_paths", payload.get("files", []))][:50]
        elif kind == "evolution.trigger_checked":
            row.update({key: payload.get(key) for key in ("triggered", "reason", "eligible") if key in payload})
        rows.append(row)
    return {"event_count": len(events), "key_events": rows}


def redact_secrets(value: str) -> str:
    """Redact common API-token forms before runner errors enter a report."""
    value = re.sub(r"(?i)\b(sk-[A-Za-z0-9_-]{12,})", "[REDACTED_TOKEN]", value)
    value = re.sub(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]{12,}", r"\1[REDACTED_TOKEN]", value)
    value = re.sub(r"(?i)\b(api[_-]?key\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", value)
    return value[:1000]


ACTIVE_TASK_STATUSES = {"created", "running", "planning", "replanning"}


class TaskExecutionTimedOut(TimeoutError):
    """An accepted Agent task stayed active beyond its execution budget."""


class Instance:
    """Run the real FastAPI app in a private process on loopback."""

    def __init__(self, config: AppConfig, *, timeout: float = 600,
                 token_budget_db: Path | None = None, max_total_tokens: int | None = None):
        self.config = config
        self.timeout = timeout
        self.token_budget_db = token_budget_db
        self.max_total_tokens = max_total_tokens
        self._temp = None
        self._process = None
        self._client = None

    async def __aenter__(self):
        self._temp = tempfile.TemporaryDirectory(prefix="evocode-eval-server-")
        work = Path(self._temp.name)
        settings = work / "config.yaml"
        settings.write_text(yaml.safe_dump(self.config.model_dump()), encoding="utf-8")
        settings.chmod(0o600)
        env = os.environ | {
            "EVOCODE_CONFIG": str(settings),
            "EVOCODE_DATA_DIR": str(Path(self.config.data_dir).resolve()),
            "EVOCODE_MODEL_URL": self.config.models.local.base_url,
            "EVOCODE_MODEL_ID": self.config.models.local.model_id,
            "EVOCODE_DOCKER_IMAGE": self.config.runtime.docker_image,
        }
        if self.token_budget_db is not None or self.max_total_tokens is not None:
            if self.token_budget_db is None or self.max_total_tokens is None:
                raise ValueError("token_budget_db and max_total_tokens must be provided together")
            env["EVOCODE_TOKEN_BUDGET_DB"] = str(Path(self.token_budget_db).resolve())
            env["EVOCODE_MAX_TOTAL_MODEL_TOKENS"] = str(self.max_total_tokens)
        try:
            with socket.socket() as listener, (work / "server.log").open("wb") as log:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
                self._process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "uvicorn", "evocode.api:create_app", "--factory",
                    "--fd", str(listener.fileno()), "--no-access-log", "--log-level", "warning",
                    cwd=ROOT, env=env, pass_fds=(listener.fileno(),), stdout=log, stderr=log,
                )
            self._client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=20)
            async with asyncio.timeout(15):
                while True:
                    if self._process.returncode is not None:
                        raise RuntimeError(f"EvoCode startup failed with exit code {self._process.returncode}")
                    try:
                        await self.request("GET", "/api/health")
                        return self
                    except httpx.TransportError:
                        await asyncio.sleep(0.1)
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        if self._client is not None:
            await self._client.aclose()
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), 12)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        if self._temp is not None:
            self._temp.cleanup()

    async def request(self, method, path, json=None, max_attempts=5):
        last_exc = None
        for attempt in range(max_attempts):
            try:
                response = await self._client.request(method, path, json=json)
                response.raise_for_status()
                return response.json()
            except (httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(0.5 * (2 ** attempt))
            except Exception:
                raise
        raise last_exc

    async def task_observations(self, task_id):
        events = await self.request("GET", f"/api/tasks/{task_id}/events")
        observed = {event["payload"].get("context_id") for event in events
                    if event["event_type"] == "model.usage"}
        tool_memory_used = any(
            event["event_type"] == "tool.completed" and
            event["payload"].get("metadata", {}).get("memory_retrieval") for event in events
        )
        messages = {}
        artifact_observations = []
        for event in events:
            if event["event_type"] != "context.assembled":
                continue
            payload = event["payload"]
            context = payload.get("node_context") or {}
            if payload.get("context_id") in observed and (tool_memory_used or context.get("skill_refs")):
                artifact = await self.request("GET", "/api/artifacts/" + payload["artifact_id"])
                artifact_messages = artifact["messages"]
                messages[payload["context_id"]] = artifact_messages
                serialized = json.dumps(artifact_messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
                artifact_observations.append({"artifact_id": payload["artifact_id"],
                    "context_id": payload["context_id"], "role": payload["role"],
                    "sha256": hashlib.sha256(serialized).hexdigest(), "message_count": len(artifact_messages)})
        return {"events": events, "trace": task_trace(events),
                "trace_artifacts": artifact_observations,
                "telemetry": task_telemetry(events, messages)}

    async def wait_idle(self):
        async with asyncio.timeout(self.timeout):
            while True:
                status = await self.request("GET", "/api/jobs")
                if not status["active"]:
                    return status
                await asyncio.sleep(0.25)

    async def run_task(self, workspace: Path, prompt: str, repository_id: str) -> dict:
        task = await self.request("POST", "/api/tasks", {
            "repo_path": str(workspace.resolve()), "prompt": prompt, "repository_id": repository_id,
        })
        path = "/api/tasks/" + task["task_id"]
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    task = await self.request("GET", path)
                    if task["status"] not in ACTIVE_TASK_STATUSES:
                        break
                    jobs = await self.request("GET", "/api/jobs")
                    if task["task_id"] not in jobs["active"]:
                        task = await self.request("GET", path)
                        if task["status"] in ACTIVE_TASK_STATUSES:
                            raise RuntimeError("EvoCode worker stopped without a terminal task state")
                        break
                    await asyncio.sleep(0.25)
        except TimeoutError:
            task = await self.request("GET", path)
            if task["status"] in ACTIVE_TASK_STATUSES:
                observations = None
                observation_failure = None
                try:
                    observations = await self.task_observations(task["task_id"])
                except Exception as exc:
                    observation_failure = redact_secrets(str(exc))
                try:
                    await self.request("POST", path + "/stop")
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 409:
                        raise
                error = TaskExecutionTimedOut(
                    f"Task {task['task_id']} exceeded its {self.timeout:g}s execution deadline"
                )
                if observations is not None:
                    error.observations = observations
                if observation_failure:
                    error.observation_failure = observation_failure
                raise error from None
        # Capture the terminal task trace before returning; callers use these
        # events to distinguish agent noncompletion from runner failure.
        jobs = await self.wait_idle()
        task = await self.request("GET", path)
        observations = await self.task_observations(task["task_id"])
        return task | observations | {
                       "worker_error": redact_secrets(str(jobs["worker_error"]))
                       if jobs["worker_error"] else None}

    async def snapshot(self, destination: Path):
        await self.wait_idle()
        await asyncio.to_thread(clone_state, Path(self.config.data_dir), destination)


async def protected_acceptance(config, workspace, oracle_root, filename, source, command):
    from evocode.app import run_protected_acceptance

    return await run_protected_acceptance(config, workspace, oracle_root, {filename: source}, command)


@asynccontextmanager
async def isolated_collection(config):
    import uuid

    original = config.collection
    name = "evocode_blackbox_" + uuid.uuid4().hex
    base = config.qdrant_url.rstrip("/") + "/collections"
    config.collection = name
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            (await client.get(base)).raise_for_status()
            try:
                yield name
            finally:
                deleted = await client.delete(f"{base}/{name}")
                if deleted.status_code != 404:
                    deleted.raise_for_status()
    finally:
        config.collection = original
