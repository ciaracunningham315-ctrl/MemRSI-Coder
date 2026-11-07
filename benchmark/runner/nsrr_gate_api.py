"""Direct, audit-only adapter for the frozen eight-candidate NSRR Gate batch."""
from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from runner.dataset_validation import inventory, source_path
from runner.nsrr_audit_tasks import load_nsrr_manifest, materialize_nsrr_task_spec


API_PATH = "/api/evolution/external-candidates/evaluate"


def api_supported(agent_root: Path) -> bool:
    """Check the local Agent source exposes the frozen direct-evaluation contract."""
    api_path = agent_root / "src/evocode/api.py"
    service_path = agent_root / "src/evocode/evolution/service.py"
    try:
        api_source = api_path.read_text(encoding="utf-8")
        service_source = service_path.read_text(encoding="utf-8")
    except OSError:
        return False
    api_requirements = (API_PATH, "queue_external_candidate_batch", "external_candidate_batch")
    service_requirements = (
        "queue_external_candidate_batch", "evaluate_external_candidate_runs",
        "get_external_candidate_batch", "EVOCODE_NSRR_AUDIT_ROOT", "EVOCODE_NSRR_FIXTURE_ROOT",
    )
    return all(item in api_source for item in api_requirements) and all(item in service_source for item in service_requirements)


class LiveNSRRGateEvaluator:
    """Run the independent candidate-evaluation API inside an isolated Agent instance.

    The Agent process receives two temporary trust-root environment variables.
    The main 54-position runner never sees NSRR runs as Task/Meta learning history.
    """

    def __init__(self, root: Path, *, backend=None, session_factory=None, poll_interval: float = 1.0,
                 timeout_seconds: float = 1800):
        self.root = root.resolve()
        self.backend = backend
        self.session_factory = session_factory
        self.poll_interval = poll_interval
        self.timeout_seconds = timeout_seconds

    def bind_backend(self, backend) -> None:
        self.backend = backend

    @property
    def fixture_root(self) -> Path:
        return self.root / ".runtime" / "nsrr_fixtures"

    def _collection(self, batch_id: str) -> str:
        return "bench_nsrr_" + hashlib.sha256(batch_id.encode("utf-8")).hexdigest()[:24]

    def _verify_qualification(self, task_id: str, manifest: dict) -> None:
        task_root = self.root / "private/gate_audit/tasks" / task_id
        evidence_path = task_root / "qualification/evidence.json"
        manifest_path = task_root / "fixture_manifest.json"
        if not evidence_path.is_file() or evidence_path.is_symlink():
            raise ValueError(f"NSRR fixture qualification is missing: {task_id}")
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        checks = evidence.get("checks", {})
        if (evidence.get("task_id") != task_id or evidence.get("passed") is not True
                or evidence.get("fixed_input_fingerprint") != manifest["fixed_input_fingerprint"]
                or evidence.get("manifest_sha256") != manifest_sha
                or evidence.get("model_calls") != 0 or evidence.get("api_calls") != 0
                or evidence.get("main_task_score_eligible") is not False
                or not checks or not all(value is True for value in checks.values())):
            raise ValueError(f"NSRR fixture qualification is stale or failed: {task_id}")

    def input_manifest(self, batch_id: str, audit_rows: list[dict]) -> dict:
        """Stable, non-secret request inputs recorded in the FormalRunner journal."""
        rows = []
        for row in audit_rows:
            task_id = row["audit_task_id"]
            manifest = load_nsrr_manifest(self.root, task_id)
            self._verify_qualification(task_id, manifest)
            candidate_file = source_path(self.root, row["candidate_manifest"])
            candidate = json.loads(candidate_file.read_text(encoding="utf-8"))
            hashes = {}
            for field in ("baseline_skill", "candidate_skill"):
                path = source_path(self.root, candidate[field])
                hashes[field] = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append({"candidate_id": row["candidate_id"], "audit_task_id": task_id,
                "baseline_skill_sha256": hashes["baseline_skill"],
                "candidate_skill_sha256": hashes["candidate_skill"],
                "fixture_manifest_sha256": hashlib.sha256((self.root / "private/gate_audit/tasks" / task_id / "fixture_manifest.json").read_bytes()).hexdigest(),
                "fixed_input_fingerprint": manifest["fixed_input_fingerprint"]})
        return {"batch_id": batch_id, "candidate_inputs": rows,
            "audit_root": ".", "fixture_root": ".runtime/nsrr_fixtures",
            "frozen_memory_collection": self._collection(batch_id),
            "freeze_manifest_sha256": audit_rows[0].get("freeze_manifest_sha256") if audit_rows else None,
            "policy_files_sent_to_gate": False}

    def _task_spec(self, task_id: str) -> dict:
        manifest = load_nsrr_manifest(self.root, task_id)
        self._verify_qualification(task_id, manifest)
        runtime_parent = self.fixture_root
        repo_path = runtime_parent / task_id
        if repo_path.exists():
            if repo_path.is_symlink() or not repo_path.is_dir():
                raise ValueError(f"unsafe existing NSRR fixture checkout: {task_id}")
            head = subprocess.check_output(["git", "-C", str(repo_path), "rev-parse", "HEAD"], text=True).strip()
            dirty = subprocess.check_output(["git", "-C", str(repo_path), "status", "--porcelain", "--untracked-files=all"], text=True)
            frozen_source = source_path(self.root, manifest["code_dir"])
            if (head != manifest["commit"] or dirty.strip()
                    or inventory(repo_path) != inventory(frozen_source)):
                raise ValueError(f"existing NSRR fixture checkout differs from its frozen inputs: {task_id}")
            protected = {target: str(source_path(self.root, relative))
                for target, relative in manifest["protected_acceptance_paths"].items()}
            spec = {"task_id": task_id, "repository_id": manifest["repository_id"],
                "repo_path": str(repo_path), "commit": head, "prompt": manifest["prompt"],
                "acceptance_command": list(manifest["acceptance_command"]),
                "acceptance_files": list(manifest["acceptance_files"]),
                "protected_acceptance_paths": protected}
        else:
            spec = materialize_nsrr_task_spec(self.root, task_id, runtime_parent)
        for field in ("reference_dir", "private_tests_dir", "task_file"):
            spec[field] = str(source_path(self.root, manifest[field]))
        spec.update({"audit_task_id": task_id,
            "fixed_input_fingerprint": manifest["fixed_input_fingerprint"]})
        return spec

    def build_request(self, batch_id: str, audit_rows: list[dict]) -> dict:
        if len(audit_rows) != 8 or len({row.get("candidate_id") for row in audit_rows}) != 8:
            raise ValueError("NSRR external Gate request requires exactly eight unique frozen candidates")
        candidates = []
        for row in audit_rows:
            task_id, candidate_id = row["audit_task_id"], row["candidate_id"]
            task_manifest = load_nsrr_manifest(self.root, task_id)
            if task_manifest["candidate_id"] != candidate_id:
                raise ValueError("NSRR fixture manifest candidate linkage differs from frozen audit row")
            candidate = json.loads(source_path(self.root, row["candidate_manifest"]).read_text(encoding="utf-8"))
            if candidate.get("candidate_id") != candidate_id or candidate.get("audit_task_id") != task_id:
                raise ValueError("NSRR candidate manifest identity differs from frozen audit row")
            skills = {}
            for arm, field in (("baseline", "baseline_skill"), ("candidate", "candidate_skill")):
                path = source_path(self.root, candidate[field])
                content = path.read_text(encoding="utf-8")
                actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                expected_hash = row.get("version_hashes", {}).get(field)
                if actual_hash != expected_hash:
                    raise ValueError(f"frozen NSRR {field} changed: {candidate_id}")
                skills[arm] = content
            task_spec = self._task_spec(task_id)
            if task_spec["repository_id"] != task_manifest["repository_id"]:
                raise ValueError("NSRR task fixture repository scope changed")
            candidates.append({"candidate_id": candidate_id,
                "repository_scope": task_spec["repository_id"],
                "baseline_skill": skills["baseline"], "candidate_skill": skills["candidate"],
                "task_specs": [task_spec]})
        return {"batch_id": batch_id, "frozen_memory_collection": self._collection(batch_id),
            "audit_fixture_root": str(self.fixture_root.resolve()), "candidates": candidates}

    @asynccontextmanager
    async def _session(self, batch_id: str, collection: str):
        if self.session_factory is not None:
            async with self.session_factory(batch_id, collection, self.fixture_root) as session:
                yield session
            return
        if self.backend is None:
            raise RuntimeError("LiveNSRRGateEvaluator must be bound to the live backend")
        state = self.backend.batch_dir / "nsrr_audit" / "state"
        await self.backend.create_empty_collection(collection)
        history = {"task_ids": [], "event_ids": []}
        manager = self.backend.instance(state, "B2", history, collection, "nsrr_audit")
        names = ("EVOCODE_NSRR_AUDIT_ROOT", "EVOCODE_NSRR_FIXTURE_ROOT")
        previous = {name: os.environ.get(name) for name in names}
        os.environ[names[0]] = str(self.root)
        os.environ[names[1]] = str(self.fixture_root.resolve())
        try:
            session = await manager.__aenter__()
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        try:
            yield session
        except BaseException as exc:
            await manager.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            await manager.__aexit__(None, None, None)

    async def evaluate(self, root: Path, batch_id: str, audit_rows: list[dict]) -> dict:
        if root.resolve() != self.root:
            raise ValueError("NSRR evaluator root changed from the formal batch root")
        body = self.build_request(batch_id, audit_rows)
        async with self._session(batch_id, body["frozen_memory_collection"]) as session:
            await session.request("POST", API_PATH, json=body)
            deadline = asyncio.get_running_loop().time() + self.timeout_seconds
            path = f"/api/evolution/external-candidates/{batch_id}"
            while True:
                response = await session.request("GET", path)
                if response.get("batch_id") != batch_id:
                    raise ValueError("NSRR Gate batch response identity mismatch")
                status = response.get("status")
                if status in {"completed", "failed"}:
                    return response
                if status not in {"queued", "running"}:
                    raise ValueError("NSRR Gate API returned an unsupported batch status")
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("NSRR Gate batch did not reach a terminal state")
                await asyncio.sleep(self.poll_interval)

