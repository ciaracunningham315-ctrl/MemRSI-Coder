"""Single-project, 18-position online engineering runner.

No model runs on import or in preflight. LiveBackend starts the real HTTP Agent
in blackbox_runtime.Instance; tests inject an explicitly offline backend. The
durable journal is an execution ledger, never an artificial results generator.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing
import copy
import csv
import contextvars
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any
import uuid

from runner.dataset_validation import STAGES, inventory, load_manifest, source_path
from runner.index_consistency import compare_index_inventory
from runner.stage_packages import materialize_stage_package, _git

GROUP_FLAGS = {
    "collector": {"raw_rag": True, "skills": False, "evolution": False},
    "A1": {"raw_rag": False, "skills": False, "evolution": False},
    "A2": {"raw_rag": True, "skills": False, "evolution": False},
    "B2": {"raw_rag": True, "skills": True, "evolution": False},
    "B3": {"raw_rag": True, "skills": True, "evolution": True},
    "B4": {"raw_rag": True, "skills": True, "evolution": True},
}
TERMINAL = {"completed", "failed", "cancelled", "interrupted", "needs_review"}
TASK_PHASES = ("planned", "prepared", "agent_running", "artifact_saved", "grading", "recorded")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_agent_runtime(agent_root: Path):
    """Use the installed Agent's public harness without copying Agent code."""
    sys.path.insert(0, str(agent_root / "src"))
    spec = importlib.util.spec_from_file_location("benchmark_blackbox_runtime", agent_root / "scripts/blackbox_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Journal:
    """Single-writer, append-preserving state transitions with atomic checkpoints."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "journal.json"
        self.data = read_json(self.path) if self.path.exists() else {"tasks": {}, "actions": {}, "transitions": []}

    def update(self, section: str, key: str, **values) -> dict:
        row = self.data[section].setdefault(key, {})
        if section == "tasks" and "phase" in values:
            previous = row.get("phase", "planned")
            if TASK_PHASES.index(values["phase"]) < TASK_PHASES.index(previous):
                raise ValueError("task phase cannot move backwards")
        row.update(values)
        self.data["transitions"].append({"section": section, "key": key,
                                         "at": time.time(), "values": values})
        atomic_json(self.path, self.data)
        return row

    async def once(self, key: str, inputs: dict, action):
        fingerprint = digest(inputs)
        row = self.data["actions"].get(key, {})
        if row and row.get("input_sha256") != fingerprint:
            raise ValueError(f"action inputs changed: {key}")
        if row.get("status") == "completed":
            return row["output"]
        self.update("actions", key, status="running", input_sha256=fingerprint, inputs=inputs)
        try:
            output = await action()
        except BaseException as exc:
            self.update("actions", key, status="interrupted", error_type=type(exc).__name__)
            raise
        self.update("actions", key, status="completed", output=output)
        return output


def execution_schedule(seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for stage in STAGES[1:]:
        groups = ["A1", "A2"]
        rng.shuffle(groups)
        rows.extend({"condition": group, "stage": stage, "attempt_id": f"{group}_{stage}"} for group in groups)
    groups = ["B2", "B3", "B4"]
    rng.shuffle(groups)
    for group in groups:
        rows.extend({"condition": group, "stage": stage, "attempt_id": f"{group}_{stage}"} for stage in STAGES)
    assert len(rows) == 18
    return rows


def collect_artifact(workspace: Path, baseline: dict, destination: Path, *, benchmark_root: Path | None = None) -> dict:
    """Save a complete, binary Git patch including ignored/untracked new files.

    A private alternate index compares against the trusted visible HEAD. It
    neither trusts the Agent's index nor mutates it, and includes deletions.
    """
    marker = destination / "manifest.json"
    if marker.exists():
        return read_json(marker)
    destination.mkdir(parents=True, exist_ok=True)
    # Source inventory rejects symlinks. Runtime caches are not code artifacts.
    actual = inventory(workspace)
    paths = sorted(actual)
    tree = destination / "files"
    tree.mkdir(exist_ok=True)
    entries = []
    for relative in paths:
        source = workspace / relative
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        mode = "100755" if source.stat().st_mode & 0o111 else "100644"
        target.chmod(0o755 if mode == "100755" else 0o644)
        entries.append({"path": relative, "sha256": actual[relative], "mode": mode})
    # Never run host Git against Agent-controlled .git/config/attributes. A
    # malicious clean filter could otherwise execute on the host during add.
    trusted = destination / "trusted_diff_repository"
    if trusted.exists():
        shutil.rmtree(trusted)
    package_root = benchmark_root or Path(__file__).resolve().parents[1]
    materialize_stage_package(package_root, baseline, trusted)
    for child in trusted.iterdir():
        if child.name != ".git":
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    shutil.copytree(tree, trusted, dirs_exist_ok=True)
    index = destination / "diff.index"
    index.unlink(missing_ok=True)
    _git(trusted, "read-tree", baseline["visible_head_sha"], index_file=index)
    # Only inventory paths enter the temporary index. rm resets tracked files
    # first so files removed by the Agent are represented as actual deletions.
    tracked = _git(trusted, "ls-files", "-z", binary=True, index_file=index)
    if tracked:
        _git(trusted, "rm", "--cached", "-r", "--ignore-unmatch", "--", ".", index_file=index)
    if paths:
        _git(trusted, "add", "--force", "--", *paths, index_file=index)
    patch = _git(trusted, "diff", "--cached", "--binary", "--full-index", baseline["visible_head_sha"], binary=True, index_file=index)
    (destination / "complete.patch").write_bytes(patch)
    index.unlink(missing_ok=True)
    shutil.rmtree(trusted)
    result = {"inventory": entries, "inventory_sha256": digest(entries),
              "patch_sha256": hashlib.sha256(patch).hexdigest(), "baseline_head": baseline["visible_head_sha"],
              "baseline_snapshot_sha256": baseline["code_snapshot_sha256"],
              "artifact_dir": str(destination), "complete_patch": str(destination / "complete.patch")}
    atomic_json(marker, result)
    return result


def restore_artifact(root: Path, manifest: dict, artifact: dict, target: Path) -> None:
    """Reapply the recorded complete patch to a verified clean stage snapshot."""
    materialize_stage_package(root, manifest, target)
    directory = Path(artifact["artifact_dir"])
    patch = (directory / "complete.patch").read_bytes()
    if hashlib.sha256(patch).hexdigest() != artifact["patch_sha256"]:
        raise ValueError("saved patch hash mismatch")
    if patch:
        _git(target, "apply", "--binary", str(directory / "complete.patch"))
    expected = {item["path"]: item["sha256"] for item in artifact["inventory"]}
    if inventory(target) != expected:
        raise ValueError("complete patch does not reproduce file inventory")
    for item in artifact["inventory"]:
        mode = "100755" if (target / item["path"]).stat().st_mode & 0o111 else "100644"
        if item["mode"] != mode:
            raise ValueError("complete patch does not reproduce file modes")


async def grade_main(root: Path, manifest: dict, artifact: dict, destination: Path,
                     image: str, *, host_engineering_only: bool = False) -> dict:
    """Private tests execute after the Agent exits, in a separate clean sandbox."""
    destination.mkdir(parents=True, exist_ok=True)
    results = {}
    for suite, key in (("acceptance", "private_tests_dir"), ("regression", "regression_tests_dir")):
        workspace = destination / (suite + "_" + uuid.uuid4().hex)
        restore_artifact(root, manifest, artifact, workspace)
        private = workspace / "_trusted_tests"
        if private.exists():
            shutil.rmtree(private) if private.is_dir() else private.unlink()
        shutil.copytree(source_path(root, manifest[key]), private)
        if not inventory(private):
            raise ValueError("empty private grader suite")
        command = ["python", "-I", "-c", "import sys,unittest;sys.path.insert(0,'.');s=unittest.defaultTestLoader.discover('_trusted_tests');n=s.countTestCases();r=unittest.TextTestRunner(verbosity=2).run(s);print('BENCHMARK_TEST_COUNT='+str(n));sys.exit(0 if n and r.wasSuccessful() else 1)"]
        if host_engineering_only:
            argv = [sys.executable, *command[1:]]
        else:
            container_name = "bench-grader-" + uuid.uuid4().hex
            argv = ["docker", "run", "--name", container_name, "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--pids-limit", "128", "--memory", "512m", "--cpus", "1",
                    "--tmpfs", "/tmp:rw,nosuid,size=64m", "-v", f"{workspace}:/workspace:rw", "-w", "/workspace", image, *command]
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(*argv, cwd=workspace,
                env={"PATH": os.environ.get("PATH", os.defpath), "DOCKER_HOST": os.environ.get("DOCKER_HOST", ""),
                     "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(destination)},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except OSError as exc:
            results[suite] = {"passed": False, "unverified": True, "exit_code": None,
                              "error_type": type(exc).__name__, "tests_run": None}
            continue
        timed_out = False
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 120)
        except TimeoutError:
            timed_out = True
            process.kill()
            output, _ = await process.communicate()
        finally:
            if not host_engineering_only:
                cleanup = await asyncio.create_subprocess_exec("docker", "rm", "-f", container_name,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await cleanup.wait()
        log = destination / (suite + ".log")
        log.write_bytes(output)
        match = re.search(rb"BENCHMARK_TEST_COUNT=(\d+)", output)
        count = int(match.group(1)) if match else None
        # Docker infrastructure errors are distinct from code/test failure.
        unverified = process.returncode in (125, 126, 127) if not host_engineering_only else False
        results[suite] = {"exit_code": process.returncode, "timed_out": timed_out,
            "tests_run": count, "passed": process.returncode == 0 and bool(count) and not timed_out,
            "unverified": unverified, "log": str(log), "argv": argv,
            "duration_seconds": time.monotonic() - started}
    is_unverified = any(row["unverified"] for row in results.values())
    return {"status": "unverified" if is_unverified else "graded",
            "solved": None if is_unverified else all(row["passed"] for row in results.values()),
            "suites": results,
            "isolation": "host_offline_test_only" if host_engineering_only else "independent_clean_docker",
            "private_feedback_exported_to_agent": False}


_INFRA_ERROR_PATTERN = re.compile(
    r"(?:\bHTTP\s+[45]\d{2}\b"
    r"|model retries exhausted"
    r"|token count API"
    r"|model_id\s*未配置"
    r"|provider does not support"
    r"|ConnectError|ConnectTimeout|ReadTimeout|WriteTimeout|PoolTimeout|TimeoutException"
    r"|RemoteProtocolError|LocalProtocolError|NetworkError|TransportError|ProxyError"
    r"|HTTPError|RequestError|ConnectionError|ConnectionResetError|ConnectionRefusedError"
    r"|ServerDisconnectedError|IncompleteRead)",
    re.IGNORECASE,
)


def extract_agent_error(output: dict) -> str:
    """提取任务终态输出中的异常信息文本。"""
    parts: list[str] = []
    state = output.get("state")
    if isinstance(state, dict) and state.get("error"):
        parts.append(str(state["error"]))
    if output.get("error"):
        parts.append(str(output["error"]))
    for event in output.get("events") or []:
        if not isinstance(event, dict):
            continue
        if event.get("event_type") in {"task.failed", "model.failed"}:
            payload = event.get("payload") or {}
            diag = payload.get("diagnostic") if isinstance(payload, dict) else None
            err = (diag.get("error") if isinstance(diag, dict) else None) or (
                payload.get("error") if isinstance(payload, dict) else None
            )
            if err:
                parts.append(str(err))
    return " | ".join(dict.fromkeys(p for p in parts if p))


def classify_infrastructure_failure(output: dict) -> dict | None:
    """识别模型服务异常或运行中断等基础设施故障，按协议归为 unverified。"""
    status = output.get("status")
    termination = output.get("termination", status)
    if status == "completed":
        return None
    if termination in {"timeout", "tool_budget_exhausted"}:
        return None
    error_text = extract_agent_error(output)
    if termination == "token_budget_exhausted":
        return {
            "reason": "token_budget_exhausted",
            "error": error_text or "global token budget exhausted during task execution",
        }
    if status in {"interrupted", "cancelled"}:
        return {
            "reason": "agent_infrastructure_interrupted",
            "error": error_text or f"agent terminated with status={status}",
        }
    if status == "failed":
        telemetry = output.get("telemetry") or {}
        model_calls = telemetry.get("model_calls")
        is_model_error = "ModelError" in error_text and "tool arguments must be an object" not in error_text
        if is_model_error or _INFRA_ERROR_PATTERN.search(error_text) or model_calls == 0:
            return {
                "reason": "model_service_error",
                "error": error_text or "task failed before any completed model call",
            }
    return None


class LiveBackend:
    """Trusted HTTP adapter; the Agent's ordinary code is not patched."""

    offline = False

    def __init__(self, root: Path, agent_root: Path, config, batch_dir: Path, max_total_tokens: int):
        self.root, self.agent_root, self.base_config = root, agent_root, config
        self.batch_dir = batch_dir
        self.runtime = load_agent_runtime(agent_root)
        from evocode.token_budget import TokenBudgetLedger
        self.ledger_path = batch_dir / "token_budget.db"
        self.ledger = TokenBudgetLedger(self.ledger_path, max_total_tokens)
        self.max_total_tokens = max_total_tokens
        self._task_batch_instances = contextvars.ContextVar(
            f"formal_task_batch_instances_{id(self)}", default=None)

    def budget(self):
        return self.ledger.snapshot()

    def configuration(self, state: Path, condition: str, history: dict, collection: str,
                      repository_id: str = "repo_api"):
        config = self.base_config.model_copy(deep=True)
        flags = GROUP_FLAGS[condition]
        config.data_dir = str(state)
        config.raw_rag.enabled = flags["raw_rag"]
        config.raw_rag.background_indexing = False
        config.raw_rag.index_when_disabled = False
        config.raw_rag.collection = collection
        config.raw_rag.history_allowed_task_ids = list(history["task_ids"])
        config.raw_rag.history_allowed_event_ids = list(history["event_ids"])
        config.skills.enabled = flags["skills"]
        config.evolution.enabled = flags["evolution"]
        config.evolution.auto_trigger_enabled = False
        config.evolution.auto_evaluate = False
        config.evolution.auto_publish = False
        config.evolution.freeze_meta_skill = condition != "B4"
        config.runtime.execution_backend = "docker"
        config.runtime.max_tool_calls = 80
        return config

    @asynccontextmanager
    async def instance(self, state, condition, history, collection, repository_id: str = "repo_api"):
        signature = (str(Path(state).resolve()), condition, collection)
        batch = self._task_batch_instances.get()
        if batch is not None:
            if signature not in batch:
                raise RuntimeError("formal batch attempted to open an unplanned Agent Instance")
            yield batch[signature]
            return
        config = self.configuration(state, condition, history, collection, repository_id)
        async with self.runtime.Instance(config, timeout=2400,
                token_budget_db=self.ledger_path, max_total_tokens=self.max_total_tokens) as instance:
            # The trigger route waits for proposal generation synchronously.
            # The harness's normal 20s control-request timeout is too short for
            # that endpoint; no extra model attempt is submitted on a timeout.
            import httpx
            instance._client.timeout = httpx.Timeout(2400)
            yield instance

    @asynccontextmanager
    async def task_batch(self, entries):
        """Start every planned service before dispatch, sharing one app per state.

        B-stage tasks share a single Application/EventStore, avoiding multiple
        Uvicorn processes writing one SQLite database. Independent A arms and
        T4 clones have distinct state paths and therefore distinct instances.
        ``Instance.__aenter__`` waits for /api/health, so all services are ready
        before the runner releases the task-dispatch barrier.
        """
        from contextlib import AsyncExitStack
        if self._task_batch_instances.get() is not None:
            raise RuntimeError("nested formal Agent task batches are not supported")
        stack = AsyncExitStack()
        instances = {}
        fingerprints = {}
        try:
            for entry in entries:
                state = entry["state"]
                condition = entry["condition"]
                collection = entry["collection"]
                repository_id = entry.get("repository_id", "repo_api")
                history = entry.get("instance_history", entry["history"])
                signature = (str(Path(state).resolve()), condition, collection)
                config_fingerprint = digest({"history": history})
                if signature in instances:
                    if fingerprints[signature] != config_fingerprint:
                        raise ValueError("one shared Agent state received conflicting batch configurations")
                    continue
                config = self.configuration(state, condition, history, collection, repository_id)
                instance = await stack.enter_async_context(self.runtime.Instance(config, timeout=2400,
                    token_budget_db=self.ledger_path, max_total_tokens=self.max_total_tokens))
                import httpx
                instance._client.timeout = httpx.Timeout(2400)
                instances[signature] = instance
                fingerprints[signature] = config_fingerprint
            token = self._task_batch_instances.set(instances)
            try:
                yield instances
            finally:
                self._task_batch_instances.reset(token)
        finally:
            await stack.aclose()

    def clone(self, source: Path, destination: Path):
        marker = destination / "benchmark_clone_complete.json"
        source_sha = self.state_fingerprint(source)
        if marker.exists():
            if read_json(marker)["source_sha256"] != source_sha:
                raise ValueError("clone source changed on recovery")
            return {"source_sha256": source_sha}
        if destination.exists():
            shutil.rmtree(destination)
        self.runtime.clone_state(source, destination)
        atomic_json(marker, {"source_sha256": source_sha})
        return {"source_sha256": source_sha}

    @staticmethod
    def state_fingerprint(state: Path):
        with closing(sqlite3.connect(state / "evocode.db")) as db:
            # Path rebasing is intentionally excluded; semantic content and
            # identities bind all sources, tasks, versions and history.
            tables = {}
            for table in ("events", "tasks", "skills", "skill_versions", "memory_chunks", "memory_fts", "evolution_runs"):
                columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
                columns = [name for name in columns if name not in {"file_path", "repo_path"}]
                rows = db.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
                tables[table] = sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
        return digest(tables)

    async def initialize(self, state: Path, collection: str):
        async with self.instance(state, "B2", {"task_ids": [], "event_ids": []}, collection) as instance:
            if await instance.request("GET", "/api/tasks"):
                raise ValueError("initial state unexpectedly contains task history")
            skills = await instance.request("GET", "/api/skills")
            meta = await instance.request("GET", "/api/evolution/active-meta")
        await self.create_empty_collection(collection)
        return {"state_sha256": self.state_fingerprint(state), "task_skills": skills, "meta": meta,
                "origin": "fresh_Agent_bootstrap_no_development_state", "history": {"task_ids": [], "event_ids": []},
                "collection": collection}

    async def qdrant(self, method: str, path: str, body=None, max_attempts=5):
        import httpx
        last_exc = None
        for attempt in range(max_attempts):
            try:
                async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                    response = await client.request(method, self.base_config.raw_rag.qdrant_url.rstrip("/") + path, json=body)
                    response.raise_for_status()
                    return response.json()["result"]
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)) or status_code in {408, 429, 500, 502, 503, 504}:
                    if attempt + 1 < max_attempts:
                        await asyncio.sleep(1.0 * (2 ** attempt))
                        continue
                raise
        raise last_exc

    async def create_empty_collection(self, name):
        import httpx
        try:
            info = await self.qdrant("GET", f"/collections/{name}")
            if info["points_count"]:
                raise ValueError("initial collection is not empty")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            size = self.base_config.raw_rag.embedding_dimensions
            if not size:
                raise ValueError("explicit embedding_dimensions required for immutable empty seed")
            await self.qdrant("PUT", f"/collections/{name}", {"vectors": {"size": size, "distance": "Cosine"}})

    async def export_collection(self, name):
        info = await self.qdrant("GET", f"/collections/{name}")
        if info.get("status") != "green":
            raise ValueError(f"frozen collection not healthy: {info.get('status')}")
        points, offset, offsets = [], None, set()
        while True:
            body = {"limit": 256, "with_payload": True, "with_vector": True}
            if offset is not None:
                body["offset"] = offset
            page = await self.qdrant("POST", f"/collections/{name}/points/scroll", body)
            points.extend(page["points"])
            offset = page.get("next_page_offset")
            if offset is None:
                break
            if str(offset) in offsets:
                raise ValueError("Qdrant scroll repeated its cursor")
            offsets.add(str(offset))
        if len(points) != info.get("points_count"):
            raise ValueError("Qdrant inventory incomplete or collection changed during export")
        points.sort(key=lambda row: str(row["id"]))
        return info, points

    async def clone_collection(self, source, destination):
        import httpx
        info, points = await self.export_collection(source)
        try:
            # A partial destination from an interrupted boundary is owned by
            # this action; replace it before replaying idempotent index jobs.
            await self.qdrant("DELETE", f"/collections/{destination}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
        await self.qdrant("PUT", f"/collections/{destination}", {"vectors": info["config"]["params"]["vectors"]})
        for start in range(0, len(points), 128):
            await self.qdrant("PUT", f"/collections/{destination}/points?wait=true", {"points": points[start:start+128]})

    async def audit(self, state: Path, descriptor: dict):
        collection = descriptor["collection"]
        info, points = await self.export_collection(collection)
        model = self.base_config.raw_rag.embedding_model
        db_path = state / "evocode.db"
        mapping = compare_index_inventory(db_path, points, {"collection": collection, "embedding_model": model, "points_complete": True})
        if not mapping["matches"]:
            raise ValueError("SQLite/Qdrant mapping mismatch")
        with closing(sqlite3.connect(db_path)) as db:
            chunks = db.execute("SELECT chunk_id,task_id,repository_id,text,metadata_json FROM memory_chunks ORDER BY chunk_id").fetchall()
            fts = db.execute("SELECT chunk_id,text FROM memory_fts ORDER BY chunk_id").fetchall()
            if fts != [(row[0], row[3]) for row in chunks]:
                raise ValueError("SQLite FTS content differs from memory_chunks")
            allowed_tasks = set(descriptor.get("history", {}).get("task_ids", []))
            allowed_events = set(descriptor.get("history", {}).get("event_ids", []))
            task_scope = descriptor.get("history", {}).get("task_ids_by_repository", {})
            event_scope = descriptor.get("history", {}).get("event_ids_by_repository", {})
            for row in chunks:
                metadata = json.loads(row[4])
                if row[1] not in allowed_tasks or not set(metadata.get("event_ids", [])) <= allowed_events:
                    raise ValueError("index chunk crosses history boundary")
                if task_scope or event_scope:
                    repository = row[2]
                    if (repository not in task_scope or row[1] not in task_scope[repository]
                            or repository not in event_scope
                            or not set(metadata.get("event_ids", [])) <= set(event_scope[repository])):
                        raise ValueError("index chunk crosses repository history scope")
        # Collection name differs across clones, so content equality has its
        # own name-independent fingerprint in addition to mapping validation.
        result = {"collection": collection, "exists": True, "health": info["status"], "mapping": mapping,
            "embedding_model": model, "rerank_model": self.base_config.raw_rag.rerank_model,
            "content_sha256": digest({"chunks": chunks, "fts": fts, "points": points}),
            "points_count": len(points), "history": descriptor.get("history", {"task_ids": [], "event_ids": []})}
        if descriptor.get("content_sha256") and descriptor["content_sha256"] != result["content_sha256"]:
            raise ValueError("frozen index was modified")
        return result

    async def task(self, state, condition, history, collection, workspace, prompt, manifest, save_identity,
                   repository_id: str = "repo_api"):
        """Recover accepted tasks by their unique workspace; never retry a model attempt."""
        if manifest.get("repository_id") != repository_id:
            raise ValueError("task manifest repository scope mismatch")
        async with self.instance(state, condition, history, collection, repository_id) as instance:
            tasks = await instance.request("GET", "/api/tasks")
            matches = [row for row in tasks if row["repo_path"] == str(workspace)]
            if len(matches) > 1:
                raise ValueError("multiple Agent attempts found for one workspace")
            if matches:
                task = matches[0]
                jobs = await instance.request("GET", "/api/jobs")
                if task["status"] not in TERMINAL and task["task_id"] not in jobs["active"]:
                    # A crashed server has no resumable in-memory job. Preserve
                    # the interrupted attempt rather than restarting generation.
                    task = await instance.request("POST", "/api/tasks/" + task["task_id"] + "/stop")
            else:
                task = await instance.request("POST", "/api/tasks", {"repo_path": str(workspace),
                    "repository_id": repository_id, "prompt": prompt})
            save_identity(task["task_id"])
            created = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            deadline = created.timestamp() + 600
            termination = None
            while True:
                task = await instance.request("GET", "/api/tasks/" + task["task_id"])
                if task["status"] in TERMINAL:
                    break
                events = await instance.request("GET", "/api/tasks/" + task["task_id"] + "/events")
                count = sum(event["event_type"] == "tool.started" for event in events)
                if count > 80:
                    raise ValueError("Agent exceeded its configured hard tool-call cap")
                if time.time() >= deadline:
                    termination = "timeout"
                    await instance.request("POST", "/api/tasks/" + task["task_id"] + "/stop")
                    break
                await asyncio.sleep(.25)
            await instance.wait_idle()
            task = await instance.request("GET", "/api/tasks/" + task["task_id"])
            observations = await instance.task_observations(task["task_id"])
            usage = [event["payload"] for event in observations["events"] if event["event_type"] == "model.usage"]
            role_usage = {}
            for role in ("planner", "coder"):
                rows = [item for item in usage if item.get("role") == role]
                totals = [item.get("provider_total_tokens") for item in rows]
                role_usage[role] = {"model_calls": len(rows), "raw_usage": rows,
                    "total_tokens": sum(totals) if totals and all(type(value) is int for value in totals) else None}
            observations["telemetry"]["role_usage"] = role_usage
            if not observations["telemetry"].get("tokens_complete"):
                observations["telemetry"]["known_reported_tokens"] = observations["telemetry"].get("reported_tokens")
                observations["telemetry"]["reported_tokens"] = None
            if any(event["event_type"] == "tool.budget_exhausted" for event in observations["events"]):
                termination = "tool_budget_exhausted"
            skills = await instance.request("GET", "/api/skills/versions")
            meta = await instance.request("GET", "/api/evolution/active-meta")
            config = await instance.request("GET", "/api/config")
            if self.budget()["stop_reason"]:
                termination = "token_budget_exhausted"
            return {**task, **observations, "termination": termination or task["status"],
                    "active_skill_versions": skills, "active_meta": meta, "actual_config": config,
                    "history": history, "collection": collection, "budget": self.budget()}

    async def boundary(self, state, condition, history, source_collection, destination_collection):
        # Complete jobs may already exist after a crash. Clone the entire prior
        # collection, then reset ONLY allowed jobs for the missing new stage;
        # idempotent IDs recover SQLite/vector partial writes without rebuild.
        _, source_points = await self.export_collection(source_collection)
        source_ids = {str(point["id"]) for point in source_points}
        await self.clone_collection(source_collection, destination_collection)
        with closing(sqlite3.connect(state / "evocode.db")) as db:
            prior_ids = {row[0] for row in db.execute("SELECT chunk_id FROM memory_chunks")}
            for event_id in history["event_ids"]:
                chunks = db.execute("SELECT chunk_id FROM memory_chunks WHERE json_extract(metadata_json,'$.event_id')=?", (event_id,)).fetchall()
                if not chunks or any(row[0] not in source_ids for row in chunks):
                    db.execute("UPDATE index_jobs SET status='pending' WHERE event_id=?", (event_id,))
            db.commit()
        rounds = []
        consecutive_failures = 0
        async with self.instance(state, condition, history, destination_collection) as instance:
            while True:
                result = await instance.request("POST", "/api/memory/index-pending", {"allowed_event_ids": history["event_ids"], "limit": 100})
                rounds.append(result)
                if result.get("deferred"):
                    raise ValueError("incremental indexing has deferred events outside allowed history")
                if result.get("failed"):
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        raise ValueError("incremental indexing failed after repeated attempts")
                    await asyncio.sleep(1.0 * consecutive_failures)
                else:
                    consecutive_failures = 0
                if not result.get("indexed") and not result.get("failed"):
                    break
        with closing(sqlite3.connect(state / "evocode.db")) as db:
            pending = []
            for event_id in history["event_ids"]:
                pending.extend(db.execute("SELECT status FROM index_jobs WHERE event_id=? AND status!='completed'", (event_id,)).fetchall())
            if pending:
                raise ValueError("allowed index jobs are incomplete")
        descriptor = await self.audit(state, {"collection": destination_collection, "history": history})
        descriptor["indexing"] = {"method": "index-pending", "allowed_event_count": len(history["event_ids"]),
                                   "previous_chunk_count": len(prior_ids), "pending_allowed": 0,
                                   "rounds": rounds, "embedding_provider_tokens": None}
        return descriptor

    async def prepare_learning(self, state, condition, history, descriptor, ids, kind, action_key, stage, gate_specs,
                               repository_id: str | None = "repo_api", meta_history_run_ids=None):
        if not ids or len(ids) != len(set(ids)) or not set(ids) <= set(history["task_ids"]):
            raise ValueError("invalid cumulative learning trace IDs")
        before = await self.audit(state, descriptor)
        if kind == "task" and not repository_id:
            raise ValueError("Task learning requires an explicit repository scope")
        async with self.instance(state, condition, history, descriptor["collection"], repository_id or "repo_api") as instance:
            runs = await instance.request("GET", "/api/evolution")
            matches = [run for run in runs if run["payload"].get("idempotency_key") == action_key]
            if len(matches) > 1:
                raise ValueError("duplicate learning action")
            if matches:
                run = matches[0]
                if (run["payload"].get("trace_task_ids") != ids or run["kind"] != kind
                        or (kind == "meta" and run["payload"].get("meta_history_run_ids") != meta_history_run_ids)):
                    raise ValueError("learning action inputs changed")
            else:
                request = {"kind": kind, "task_id": ids[-1], "trace_task_ids": ids,
                    "idempotency_key": action_key}
                if kind == "meta" and meta_history_run_ids is not None:
                    request["meta_history_run_ids"] = meta_history_run_ids
                run = await instance.request("POST", "/api/evolution", request)
            # trigger is synchronous. A leftover created run after server
            # shutdown is ambiguous and must not submit another proposal.
            if run["status"] == "created":
                raise RuntimeError("proposal interrupted before durable result; manual reconciliation required")
            if kind == "meta":
                payload = run["payload"]
                expected = meta_history_run_ids
                if (not isinstance(expected, list) or payload.get("meta_summary_run_ids") != expected
                        or payload.get("meta_history_truncated")):
                    raise ValueError("Meta review did not include its complete bounded Task feedback")
            candidate = run["payload"].get("candidate")
            candidate_version_id = None
            status = run["status"]
            if candidate:
                version = candidate
                if version.get("level") == "general":
                    # chain_integration declares an empty General gate matrix.
                    # Do not silently substitute this project's Project gate.
                    status = "not_evaluated"
                elif kind == "task" and (version.get("level") != "project" or version.get("scope_id") != repository_id):
                    raise ValueError("Task candidate repository scope mismatch")
                candidate_version_id = version.get("version_id")
            proposal_snapshot = {"pinned_skill_versions": run["payload"].get("pinned_skill_versions", []),
                "base_meta": run["payload"].get("base_meta")}
            proposal_snapshot["sha256"] = digest(proposal_snapshot)
        after = await self.audit(state, descriptor)
        return {"run": run, "run_id": run["run_id"], "status": status,
            "candidate_version_id": candidate_version_id, "evaluation_passed": None,
            "trace_task_ids": ids, "meta_history_run_ids": meta_history_run_ids,
            "kind": kind, "stage": stage, "proposal_snapshot": proposal_snapshot,
            "frozen_index_before": before, "frozen_index_after": after,
            "gate_task_ids": [spec["task_id"] for spec in gate_specs], "budget": self.budget()}

    async def evaluate_learning(self, state, condition, history, descriptor, prepared, gate_specs, kind,
                                repository_id: str | None = "repo_api"):
        run_id = prepared["run_id"]
        before = await self.audit(state, descriptor)
        async with self.instance(state, condition, history, descriptor["collection"], repository_id or "repo_api") as instance:
            run = await instance.request("GET", f"/api/evolution/{run_id}")
            if run["status"] == "candidate":
                await instance.request("POST", f"/api/evolution/{run_id}/evaluate", {
                    "task_specs": gate_specs, "frozen_memory_collection": descriptor["collection"]})
                await instance.wait_idle()
                run = await instance.request("GET", f"/api/evolution/{run_id}")
            elif run["status"] == "evaluating":
                jobs = await instance.request("GET", "/api/jobs")
                if "eval:" + run_id not in jobs["active"]:
                    raise RuntimeError("evaluation interrupted without a terminal report; no automatic rerun")
                await instance.wait_idle()
                run = await instance.request("GET", f"/api/evolution/{run_id}")
            if run["status"] in {"candidate", "evaluating", "created"}:
                raise RuntimeError("Gate evaluation has not reached a durable terminal state")
            if run["status"] == "published":
                raise RuntimeError("Skill was published before the learning evaluation barrier")
        after = await self.audit(state, descriptor)
        return {"run": run, "run_id": run_id, "status": run["status"],
            "candidate_version_id": prepared.get("candidate_version_id"),
            "evaluation_passed": run.get("payload", {}).get("evaluation", {}).get("passed") is True,
            "proposal_snapshot": prepared.get("proposal_snapshot"),
            "trace_task_ids": prepared.get("trace_task_ids"),
            "meta_history_run_ids": prepared.get("meta_history_run_ids"),
            "kind": kind, "frozen_index_before": before, "frozen_index_after": after,
            "gate_task_ids": [spec["task_id"] for spec in gate_specs], "budget": self.budget()}

    async def publish_learning(self, state, condition, history, descriptor, evaluation, kind,
                               repository_id: str | None = "repo_api"):
        run_id = evaluation["run_id"]
        async with self.instance(state, condition, history, descriptor["collection"], repository_id or "repo_api") as instance:
            run = await instance.request("GET", f"/api/evolution/{run_id}")
            if run["status"] == "evaluated" and run.get("payload", {}).get("evaluation", {}).get("passed") is True:
                run = await instance.request("POST", f"/api/evolution/{run_id}/publish")
            elif run["status"] == "published":
                pass
            elif run["status"] in {"candidate", "evaluating", "created"}:
                raise RuntimeError("Skill publication attempted before all Gate evaluations were terminal")
            skills = await instance.request("GET", "/api/skills/versions")
            meta = await instance.request("GET", "/api/evolution/active-meta")
        return {**evaluation, "run": run, "status": run["status"],
            "evaluation_passed": run.get("payload", {}).get("evaluation", {}).get("passed") is True,
            "skill_versions": skills, "active_meta": meta, "budget": self.budget()}

    async def learn(self, state, condition, history, descriptor, ids, kind, action_key, stage, gate_specs,
                    repository_id: str | None = "repo_api", meta_history_run_ids=None):
        """Sequential compatibility wrapper used by the repo_api chain runner."""
        prepared = await self.prepare_learning(state, condition, history, descriptor, ids,
            kind, action_key, stage, gate_specs, repository_id=repository_id,
            meta_history_run_ids=meta_history_run_ids)
        if prepared.get("status") != "candidate" or not prepared.get("candidate_version_id"):
            return prepared
        evaluated = await self.evaluate_learning(state, condition, history, descriptor,
            prepared, gate_specs, kind, repository_id=repository_id)
        return await self.publish_learning(state, condition, history, descriptor,
            evaluated, kind, repository_id=repository_id)

    async def history_evidence(self, state, task_ids, workspace, repository_id: str = "repo_api"):
        """Recheck only metadata extracted from actual collected tool events.

        This exports observations, including missing evidence, without inventing
        a replacement diff or making semantic-applicability claims.
        """
        from evocode.memory.service import MemoryService
        service = MemoryService(None, self.base_config)
        with closing(sqlite3.connect(state / "evocode.db")) as db:
            rows = db.execute("SELECT chunk_id,task_id,metadata_json FROM memory_chunks ORDER BY chunk_id").fetchall()
        observations = []
        for chunk_id, task_id, serialized in rows:
            if task_id not in task_ids:
                continue
            metadata = json.loads(serialized)
            if not any(metadata.get(key) for key in ("file_paths", "symbols", "content_evidence")):
                continue
            actual = service._applicability(metadata, {"repository_id": repository_id, "repo_path": str(workspace)})
            observations.append({"chunk_id": chunk_id, "task_id": task_id,
                                 "metadata": metadata, "applicability": actual})
        statuses = {key: sorted({row["applicability"]["checks"].get(key, {}).get("status", "unknown")
                                for row in observations}) for key in ("paths", "symbols", "content", "git")}
        return {"source": "actual_collector_index_metadata", "observations": observations,
                "observed_statuses": statuses, "evidence_present": bool(observations),
                "boundary": "Observed structural applicability only; no synthetic evidence substituted."}


class ChainRunner:
    """Frozen repo_api chain; every mutable state and action has a stable owner."""

    def __init__(self, root: Path, batch_id: str, backend, *, gate_provider=None, grader=None,
                 batch_metadata: dict | None = None, repository_id: str = "repo_api",
                 formal_results: bool = False):
        self.root, self.batch_id, self.backend = root.resolve(), batch_id, backend
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", batch_id):
            raise ValueError("unsafe batch_id")
        self.offline = bool(backend.offline)
        self.is_formal = formal_results
        self.repository_id = repository_id
        self.formal_results = formal_results and not self.offline
        offline_root = ".runtime/offline_formal_tests" if formal_results else ".runtime/offline_contract_tests"
        work_root = ".runtime/formal_chains" if formal_results else ".runtime/chains"
        self.results = self.root / (offline_root if self.offline else "results") / batch_id
        self.work = self.root / work_root / batch_id
        self.results.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.results)
        self.manifests = {stage: load_manifest(self.root, stage, repository_id) for stage in STAGES}
        self.schedule = execution_schedule()
        self.gate_provider = gate_provider
        self.grader = grader
        self.metadata = batch_metadata or {}

    def collection(self, owner: str):
        return "bench_" + self.batch_id + "_" + owner

    def check_budget(self):
        state = self.backend.budget()
        if state.get("stop_reason") or state.get("remaining_admissible_tokens", 1) <= 0:
            raise BudgetStopped(state)

    async def action(self, key, inputs, callback):
        return await self.journal.once(key, inputs, callback)

    async def clone(self, source, destination, owner):
        async def do_clone():
            return self.backend.clone(source, destination)
        return await self.action(owner + ":clone", {"source": str(source), "destination": str(destination)}, do_clone)

    async def freeze(self, source, owner, history, descriptor):
        destination = self.work / "snapshots" / owner
        await self.clone(source, destination, owner + ":snapshot")
        async def audit():
            return await self.backend.audit(destination, descriptor)
        checked = await self.action(owner + ":snapshot:audit", {"snapshot": str(destination), "descriptor": descriptor}, audit)
        return {"state": str(destination), "history": history, "index": checked}

    def _task_attempt_inputs(self, condition, stage, state, history, descriptor, *,
                             repository_id, attempt):
        manifest = load_manifest(self.root, stage, repository_id)
        inputs = {"condition": condition, "stage": stage, "repository_id": repository_id,
                  "task_id": manifest["task_id"],
                  "flags": GROUP_FLAGS[condition], "history": history, "index": descriptor,
                  "state": str(state), "code_snapshot_sha256": manifest["code_snapshot_sha256"],
                  "visible_head_sha": manifest["visible_head_sha"], "allowed_ancestor_shas": manifest["allowed_ancestor_shas"],
                  "git_stage_package_sha256": manifest["git_stage_package_sha256"],
                  "private_test_fingerprints": {key: inventory(source_path(self.root, manifest[key]))
                                                for key in ("private_tests_dir", "regression_tests_dir")}}
        return manifest, inputs

    async def prepare_task(self, condition, stage, state, history, descriptor, *, scored=True,
                           repository_id: str | None = None, attempt_id: str | None = None,
                           journal_metadata: dict | None = None):
        """Prepare a task workspace before any member of a wave is dispatched."""
        repository_id = repository_id or self.repository_id
        attempt = attempt_id or f"{condition}_{stage}"
        manifest, inputs = self._task_attempt_inputs(condition, stage, state, history,
            descriptor, repository_id=repository_id, attempt=attempt)
        if journal_metadata:
            inputs["journal_metadata"] = journal_metadata
        row = self.journal.data["tasks"].get(attempt, {})
        if row.get("inputs") and row["inputs"] != inputs:
            raise ValueError("attempt inputs changed during recovery")
        if row.get("phase") in ("artifact_saved", "grading", "recorded"):
            return
        workspace = self.work / "workspaces" / attempt
        if TASK_PHASES.index(row.get("phase", "planned")) < TASK_PHASES.index("prepared"):
            if workspace.exists():
                shutil.rmtree(workspace)
            workspace.parent.mkdir(parents=True, exist_ok=True)
            check = materialize_stage_package(self.root, manifest, workspace)
            self.journal.update("tasks", attempt, phase="prepared", inputs=inputs,
                package_check=check, **(journal_metadata or {}))

    async def start_task(self, condition, stage, state, history, descriptor, *, scored=True,
                         repository_id: str | None = None, attempt_id: str | None = None,
                         journal_metadata: dict | None = None, audit_before=True, audit_after=True):
        """Materialize and run one Agent attempt, stopping before artifact/grading work.

        Formal stage scheduling uses this method for every member of a wave, waits
        for all Agent attempts to reach terminal records, then invokes
        ``finalize_task`` in fixed order. This keeps grading from overlapping a
        slower Agent in the same stage.
        """
        repository_id = repository_id or self.repository_id
        attempt = attempt_id or f"{condition}_{stage}"
        row = self.journal.data["tasks"].get(attempt, {})
        if row.get("phase") in ("artifact_saved", "grading", "recorded"):
            return row.get("result")
        self.check_budget()
        workspace = self.work / "workspaces" / attempt
        artifact_directory = self.results / "attempts" / attempt
        manifest, inputs = self._task_attempt_inputs(condition, stage, state, history,
            descriptor, repository_id=repository_id, attempt=attempt)
        if journal_metadata:
            inputs["journal_metadata"] = journal_metadata
        await self.prepare_task(condition, stage, state, history, descriptor, scored=scored,
            repository_id=repository_id, attempt_id=attempt, journal_metadata=journal_metadata)
        row = self.journal.data["tasks"][attempt]
        if TASK_PHASES.index(row["phase"]) < TASK_PHASES.index("artifact_saved"):
            self.journal.update("tasks", attempt, phase="agent_running", started_at=row.get("started_at", time.time()),
                **(journal_metadata or {}))
            if audit_before:
                await self.backend.audit(state, descriptor)

            def save_identity(task_id):
                self.journal.update("tasks", attempt, agent_task_id=task_id)

            output_file = artifact_directory / "agent_output.json"
            artifact_directory.mkdir(parents=True, exist_ok=True)
            if output_file.exists():
                output = read_json(output_file)
            else:
                if self.journal.data["tasks"].get(attempt, {}).get("agent_dispatched_at") is None:
                    self.journal.update("tasks", attempt, agent_dispatched_at=time.time())
                task_args = (state, condition, history, descriptor["collection"], workspace,
                    source_path(self.root, manifest["task_file"]).read_text(), manifest, save_identity)
                if self.is_formal:
                    output = await self.backend.task(*task_args, repository_id=repository_id)
                else:
                    output = await self.backend.task(*task_args)
                if output["status"] not in TERMINAL:
                    raise ValueError("Agent did not reach a durable terminal state")
                atomic_json(output_file, output)
            # Current task writes never enter the frozen index, even on failure.
            if output.get("status") not in TERMINAL:
                raise ValueError("saved Agent attempt has not reached a durable terminal state")
            self.journal.update("tasks", attempt, agent_terminal=True,
                agent_terminal_at=time.time(), agent_status=output.get("status"),
                agent_task_created_at=output.get("created_at"),
                agent_output=str(output_file))
            if audit_after:
                await self.backend.audit(state, descriptor)
        return {"attempt_id": attempt, "agent_output": str(artifact_directory / "agent_output.json"),
                "workspace": str(workspace), "state": str(state), "condition": condition,
                "stage": manifest["stage"], "repository_id": repository_id, "scored": scored,
                "inputs": inputs}

    async def finalize_task(self, condition, stage, state, history, descriptor, *, scored=True,
                            repository_id: str | None = None, attempt_id: str | None = None,
                            journal_metadata: dict | None = None):
        """Collect a terminal Agent output and grade it without dispatching again."""
        repository_id = repository_id or self.repository_id
        attempt = attempt_id or f"{condition}_{stage}"
        row = self.journal.data["tasks"].get(attempt, {})
        if row.get("phase") == "recorded":
            return row["result"]
        manifest, inputs = self._task_attempt_inputs(condition, stage, state, history,
            descriptor, repository_id=repository_id, attempt=attempt)
        if journal_metadata:
            inputs["journal_metadata"] = journal_metadata
        if row.get("inputs") and row["inputs"] != inputs:
            raise ValueError("attempt inputs changed during recovery")
        workspace = self.work / "workspaces" / attempt
        artifact_directory = self.results / "attempts" / attempt
        output_file = artifact_directory / "agent_output.json"
        if not output_file.exists():
            raise RuntimeError("cannot finalize an Agent attempt without its durable terminal output")
        output = read_json(output_file)
        if output.get("status") not in TERMINAL:
            raise RuntimeError("cannot finalize an Agent attempt before terminal status")
        if TASK_PHASES.index(row.get("phase", "planned")) < TASK_PHASES.index("artifact_saved"):
            try:
                artifact = collect_artifact(workspace, manifest, artifact_directory / "artifact", benchmark_root=self.root)
            except (ValueError, OSError) as exc:
                artifact = {"invalid_artifact": type(exc).__name__, "error": str(exc)}
            self.journal.update("tasks", attempt, phase="artifact_saved", agent_output=str(output_file),
                artifact=artifact, agent_terminal=True, agent_status=output.get("status"),
                agent_task_created_at=output.get("created_at"), **(journal_metadata or {}))
        row = self.journal.data["tasks"][attempt]
        output = read_json(Path(row["agent_output"]))
        artifact = row["artifact"]
        self.journal.update("tasks", attempt, phase="grading")
        grade_file = artifact_directory / "private_grade.json"
        infra_failure = classify_infrastructure_failure(output)
        if grade_file.exists():
            grade = read_json(grade_file)
        elif not scored:
            # Collector never executes or receives main private tests.
            grade = {"status": "not_run", "solved": None, "reason": "public_history_collection"}
        elif infra_failure is not None:
            grade = {"status": "unverified", "solved": None, **infra_failure}
        elif artifact.get("invalid_artifact"):
            grade = {"status": "graded", "solved": False, "reason": "invalid_complete_artifact"}
        elif self.grader is not None:
            grade = await self.grader(self.root, manifest, artifact, artifact_directory / "grader")
        else:
            grade = await grade_main(self.root, manifest, artifact, artifact_directory / "grader",
                                     self.backend.base_config.runtime.docker_image)
        atomic_json(grade_file, grade)
        failed_budget = output.get("termination") in {"timeout", "tool_budget_exhausted", "token_budget_exhausted"}
        is_graded = grade.get("status") == "graded"
        solved = (bool(grade.get("solved")) and not failed_budget) if (scored and is_graded) else None
        result = {"attempt_id": attempt, "condition": condition, "stage": manifest["stage"], "stage_directory": stage,
                  "repository_id": repository_id, "repeat_id": 1, "seed": 42,
                  "agent_task_id": output["task_id"],
                  "agent_task_created_at": output.get("created_at"),
                  "agent_status": output["status"], "termination": output.get("termination", output["status"]),
                  "scored": scored, "formal_results": self.formal_results, "offline_contract_test": self.offline,
                  "grade": grade, "solved": solved,
                  "telemetry": output.get("telemetry", {}), "inputs": inputs,
                  "artifact": artifact, "agent_output": str(Path(row["agent_output"])), "private_grade": str(grade_file),
                  "dispatch": {key: row[key] for key in ("wave_id", "pair_id", "pair_order", "pair_dispatched_at",
                      "agent_dispatched_at", "agent_terminal_at", "agent_task_created_at") if key in row}}
        atomic_json(artifact_directory / "result.json", result)
        self.journal.update("tasks", attempt, phase="recorded", result=result)
        return result

    async def task(self, condition, stage, state, history, descriptor, *, scored=True,
                   repository_id: str | None = None, attempt_id: str | None = None,
                   journal_metadata: dict | None = None):
        await self.start_task(condition, stage, state, history, descriptor, scored=scored,
            repository_id=repository_id, attempt_id=attempt_id, journal_metadata=journal_metadata)
        return await self.finalize_task(condition, stage, state, history, descriptor, scored=scored,
            repository_id=repository_id, attempt_id=attempt_id, journal_metadata=journal_metadata)

    def history_after(self, history, result, repository_id: str | None = None):
        output = read_json(Path(result["agent_output"]))
        events = output["events"]
        task_id = result["agent_task_id"]
        if task_id in history["task_ids"]:
            raise ValueError("duplicate task in stage history")
        if any(event.get("task_id") != task_id for event in events):
            raise ValueError("observation contains another task's events")
        event_ids = [event["event_id"] for event in events]
        if len(set(event_ids)) != len(event_ids) or set(event_ids) & set(history["event_ids"]):
            raise ValueError("duplicate event in stage history")
        updated = {"task_ids": [*history["task_ids"], task_id],
                   "event_ids": [*history["event_ids"], *event_ids]}
        if repository_id is not None:
            task_scopes = copy.deepcopy(history.get("task_ids_by_repository", {}))
            event_scopes = copy.deepcopy(history.get("event_ids_by_repository", {}))
            task_scopes.setdefault(repository_id, []).append(task_id)
            event_scopes.setdefault(repository_id, []).extend(event_ids)
            updated["task_ids_by_repository"] = task_scopes
            updated["event_ids_by_repository"] = event_scopes
        return updated

    async def index_stage(self, state, condition, stage, history, old):
        collection = self.collection(condition + "_" + stage)
        key = f"{condition}:{stage}:index"
        async def boundary():
            self.check_budget()
            return await self.backend.boundary(state, condition, history, old["collection"], collection)
        return await self.action(key, {"state": str(state), "history": history, "source_index": old,
                                       "destination_collection": collection}, boundary)

    def _meta_history_run_order(self, condition: str) -> list[str]:
        result = []
        for stage in STAGES[:STAGES.index("t2_transfer") + 1]:
            action_key = f"{self.batch_id}:{condition}:{stage}:task"
            action = self.journal.data["actions"].get(action_key, {})
            output = action.get("output") or {}
            run_id = output.get("run_id") or (output.get("run") or {}).get("run_id")
            if not run_id:
                raise ValueError(f"{condition} Meta history is missing Task learning run: {stage}")
            result.append(run_id)
        return result

    async def learning(self, state, condition, stage, history, descriptor, kind="task", *,
                       repository_id: str | None = None, trace_task_ids: list[str] | None = None,
                       gate_specs: list[dict] | None = None, action_key: str | None = None,
                       meta_history_run_ids: list[str] | None = None):
        self.check_budget()
        repository_id = repository_id or self.repository_id
        action_key = action_key or f"{self.batch_id}:{condition}:{stage}:{kind}"
        manifest_stage = self.manifests[stage]["stage"]
        sig_params = inspect.signature(self.backend.learn).parameters
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_params.values())
        meta_run_ids = meta_history_run_ids
        if kind == "meta" and meta_run_ids is None and (self.is_formal or has_varkw or "meta_history_run_ids" in sig_params):
            meta_run_ids = self._meta_history_run_order(condition)
        async def learn():
            if self.gate_provider is None:
                from runner.gate_tasks import build_gate_specs
                provider = build_gate_specs
            else:
                provider = self.gate_provider
            gate_directory = self.work / "gates" / condition / stage / kind
            if gate_specs is not None:
                specs = list(gate_specs)
            elif repository_id is not None and self.is_formal:
                specs = provider(self.root, manifest_stage, kind, gate_directory, repository_id=repository_id)
                # Fixed regression joins each candidate comparison and never T4.
                specs += provider(self.root, manifest_stage, "regression", gate_directory / "regression",
                                  repository_id=repository_id)
            else:
                specs = provider(self.root, manifest_stage, kind, gate_directory)
                # Fixed regression joins each candidate comparison and never T4.
                specs += provider(self.root, manifest_stage, "regression", gate_directory / "regression")
            if not specs or len({row["task_id"] for row in specs}) != len(specs):
                raise ValueError("invalid Gate/regression task matrix")
            ids = trace_task_ids if trace_task_ids is not None else history["task_ids"]
            learn_args = (state, condition, history, descriptor, ids, kind, action_key, stage, specs)
            if self.is_formal:
                return await self.backend.learn(*learn_args, repository_id=repository_id, meta_history_run_ids=meta_run_ids)
            # 按后端签名传递可选参数，兼容在线后端与离线测试桩
            extra_kwargs = {
                k: v
                for k, v in (("repository_id", repository_id), ("meta_history_run_ids", meta_run_ids))
                if has_varkw or k in sig_params
            }
            return await self.backend.learn(*learn_args, **extra_kwargs)
        return await self.action(action_key, {"state": str(state), "condition": condition, "stage": stage,
            "history": history, "index": descriptor, "kind": kind}, learn)

    async def run(self):
        # A file lock prevents two CLIs from duplicating a batch's accepted task.
        with (self.results / ".runner.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("another runner owns this batch") from None
            return await self._run_locked()

    async def _run_locked(self):
        protocol_path = self.root / "configs" / "protocol.yaml"
        protocol_id = "evocode-benchmark-2026-11-06-r2"
        if protocol_path.is_file():
            import yaml
            protocol_id = yaml.safe_load(protocol_path.read_text(encoding="utf-8")).get("protocol_id", protocol_id)
        manifest = {"protocol_id": protocol_id, "profile": "chain_integration",
                    "formal_results": False, "batch_id": self.batch_id, "offline_contract_test": self.offline,
                    "seed": 42, "seed_supported": False, "planned_main_tasks": 18,
                    "schedule": self.schedule, "metadata": self.metadata,
                    "initial_state_origin": "fresh bootstrap; empty Task Skill library; built-in Meta V1"}
        manifest_file = self.results / "batch_manifest.json"
        if manifest_file.exists() and read_json(manifest_file) != manifest:
            raise ValueError("batch manifest changed; use a new batch_id")
        atomic_json(manifest_file, manifest)
        for row in self.schedule:
            if row["attempt_id"] not in self.journal.data["tasks"]:
                self.journal.update("tasks", row["attempt_id"], phase="planned", schedule=row)
        stop = None
        try:
            initial_state = self.work / "initial_state"
            initial = await self.action("initial:bootstrap", {"state": str(initial_state)},
                lambda: self.backend.initialize(initial_state, self.collection("initial")))
            async def initial_audit():
                return await self.backend.audit(initial_state, {"collection": initial["collection"], "history": initial["history"]})
            initial_index = await self.action("initial:index_audit", initial, initial_audit)
            collector = self.work / "states" / "collector"
            await self.clone(initial_state, collector, "collector")
            history, descriptor = initial["history"], initial_index
            public = {}
            for stage in STAGES[:3]:
                result = await self.task("collector", stage, collector, history, descriptor, scored=False)
                history = self.history_after(history, result)
                descriptor = await self.index_stage(collector, "collector", stage, history, descriptor)
                public[stage] = await self.freeze(collector, "collector_" + stage, history, descriptor)
            # T3 revalidation is based on actual T1/T2 events, never a fabricated
            # extractor fixture. Missing observations are retained explicitly.
            async def revalidate():
                target = self.work / "history_revalidation_t3"
                if not target.exists():
                    materialize_stage_package(self.root, self.manifests[STAGES[2]], target)
                return await self.backend.history_evidence(Path(public[STAGES[1]]["state"]),
                    public[STAGES[1]]["history"]["task_ids"], target)
            await self.action("collector:actual_history_revalidation", {"snapshot": public[STAGES[1]]}, revalidate)
            for row in self.schedule:
                condition, stage = row["condition"], row["stage"]
                if not condition.startswith("A"):
                    continue
                seed = public[STAGES[STAGES.index(stage) - 1]]
                state = self.work / "states" / row["attempt_id"]
                await self.clone(Path(seed["state"]), state, row["attempt_id"])
                await self.task(condition, stage, state, seed["history"], seed["index"])
            conditions = list(dict.fromkeys(row["condition"] for row in self.schedule if row["condition"].startswith("B")))
            for condition in conditions:
                state = self.work / "states" / condition
                await self.clone(initial_state, state, condition)
                history, descriptor = initial["history"], initial_index
                frozen = None
                for stage in STAGES:
                    if stage == STAGES[-1]:
                        heldout = self.work / "states" / (condition + "_heldout")
                        await self.clone(Path(frozen["state"]), heldout, condition + ":heldout")
                        await self.task(condition, stage, heldout, frozen["history"], frozen["index"])
                        continue
                    result = await self.task(condition, stage, state, history, descriptor)
                    history = self.history_after(history, result)
                    descriptor = await self.index_stage(state, condition, stage, history, descriptor)
                    if condition in ("B3", "B4"):
                        await self.learning(state, condition, stage, history, descriptor)
                    if stage == STAGES[1]:
                        if condition == "B4":
                            await self.learning(state, condition, stage, history, descriptor, "meta")
                        else:
                            self.journal.update("actions", condition + ":transfer:meta_barrier", status="completed",
                                output={"status": "fixed_v1", "trace_task_ids": history["task_ids"]})
                    frozen = await self.freeze(state, condition + "_" + stage, history, descriptor)
        except BudgetStopped as exc:
            stop = {"reason": "token_budget_limit", "budget": exc.state}
        except BaseException as exc:
            stop = {"reason": "runner_interrupted", "error_type": type(exc).__name__}
            self.write_summary(stop)
            raise
        return self.write_summary(stop)

    def write_summary(self, stop):
        results = [row["result"] for row in self.journal.data["tasks"].values()
                   if row.get("phase") == "recorded" and row["result"]["scored"]]
        verified = [row for row in results if row["grade"]["status"] == "graded"]
        unverified = [row for row in results if row["grade"]["status"] == "unverified"]
        summary = {"kind": "offline_contract_test" if self.offline else "online_chain_integration",
            "formal_results": False, "batch_id": self.batch_id, "planned": 18, "recorded": len(results),
            "verified": len(verified), "unverified": len(unverified),
            "coverage": len(verified) / 18, "solved": sum(bool(row["solved"]) for row in verified),
            "solve_rate": sum(bool(row["solved"]) for row in verified) / len(verified) if verified else None,
            "complete": len(results) == 18 and (len(verified) == 18 or self.offline) and not stop, "stop": stop,
            "budget": self.backend.budget(), "results": results,
            "nsrr": {"status": "not_run", "value": None},
            "boundary": "Offline fake transport checks; no model calls or ability scores." if self.offline
                else "One repo_api engineering batch; not a formal three-project experiment."}
        atomic_json(self.results / "summary.json", relative_paths(summary, self.root))
        if not self.offline:
            report_directory = self.root / "reports" / self.batch_id
            report_directory.mkdir(parents=True, exist_ok=True)
            with (report_directory / "main_tasks.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=["attempt_id", "condition", "stage", "agent_task_id", "agent_status", "termination", "solved", "grade_status"])
                writer.writeheader()
                for row in results:
                    writer.writerow({**{name: row[name] for name in writer.fieldnames if name != "grade_status"}, "grade_status": row["grade"]["status"]})
            lines = ["# repo_api 在线工程闭环", "", "本批次为 chain_integration，formal_results=false。", "",
                     f"计划 18；记录 {len(results)}；有效判分 {len(verified)}；成功 {summary['solved']}；覆盖率 {summary['coverage']:.1%}。", "",
                     "| 组别 | 阶段 | 终态 | 独立判分 | 成功 |", "|---|---|---|---|---|"]
            lines.extend(f"| {row['condition']} | {row['stage']} | {row['termination']} | {row['grade']['status']} | {row['solved']} |" for row in results)
            lines += ["", "NSRR 未运行；正式跨项目统计不适用。", "", f"停止原因：{stop['reason'] if stop else 'none'}。", ""]
            (report_directory / "summary.md").write_text("\n".join(lines))
        return summary


class BudgetStopped(RuntimeError):
    def __init__(self, state):
        self.state = state
        super().__init__("global token budget stopped dispatch")


def relative_paths(value, root):
    if isinstance(value, dict):
        return {key: relative_paths(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [relative_paths(item, root) for item in value]
    if isinstance(value, str) and value.startswith(str(root) + "/"):
        return str(Path(value).relative_to(root))
    return value

