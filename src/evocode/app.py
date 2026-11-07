from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import shutil
import shlex
import sqlite3
import time
import uuid
from pathlib import Path

from evocode.config import AppConfig
from evocode.contracts import TaskSpec
from evocode.llm import ModelClient


def _verified_coder_skill_injections(store, events, registry):
    """Prove a versioned skill was in a Coder request that reached the model.

    Selection events alone are not evidence of exposure: the context assembler
    can omit optional skill content to fit the input budget. Instead, inspect
    the persisted context artifact and pair its exact skill payload with a
    model.usage event for the same context id.
    """
    model_context_ids = {
        event['payload'].get('context_id')
        for event in events
        if event['event_type'] == 'model.usage'
        and event['payload'].get('role') == 'coder'
        and event['payload'].get('context_id')
    }
    if not model_context_ids:
        return []

    prefix = 'Applicable versioned skills (subject to task constraints):\n'
    verified = []
    for event in events:
        if event['event_type'] != 'context.assembled':
            continue
        payload = event['payload']
        context_id = payload.get('context_id')
        if context_id not in model_context_ids or payload.get('role') != 'coder':
            continue
        artifact_id = payload.get('artifact_id')
        if not artifact_id:
            continue
        try:
            artifact = store.get_artifact(artifact_id)
            context = json.loads(Path(artifact['path']).read_text())
        except (KeyError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
            continue
        if context.get('context_id') != context_id or context.get('node') != 'coder':
            continue
        for message in context.get('messages', []):
            content = message.get('content') if isinstance(message, dict) else None
            if not isinstance(content, str) or not content.startswith(prefix):
                continue
            try:
                skill_items = json.loads(content[len(prefix):])
            except json.JSONDecodeError:
                continue
            if not isinstance(skill_items, list):
                continue
            for item in skill_items:
                if not isinstance(item, dict) or not item.get('version_id'):
                    continue
                try:
                    version = registry.load(item['version_id'])
                except (KeyError, OSError, ValueError):
                    continue
                content_value = item.get('content')
                content_hash = hashlib.sha256(content_value.encode()).hexdigest() if isinstance(content_value, str) else None
                if (not content_hash
                        or content_hash != version.get('content_sha256')
                        or item.get('content_sha256') != content_hash
                        or content_value != version.get('content')):
                    continue
                verified.append({
                    'version_id': item['version_id'],
                    'content_sha256': content_hash,
                    'context_id': context_id,
                    'injection_verified': True,
                    'model_call_observed': True,
                })
    return verified


class Application:
    def __init__(self, config: AppConfig, llm=None):
        from evocode.agent import AgentController
        from evocode.evolution import EvolutionService
        from evocode.memory import MemoryService
        from evocode.persistence import EventStore
        from evocode.skills import SkillRegistry
        self.config = config
        self.store = EventStore(config.sqlite_path, Path(config.data_dir) / "artifacts")
        self.llm = llm or ModelClient(config)
        self.memory = MemoryService(self.store, config)
        self.skills = SkillRegistry(self.store, config.data_dir)
        self.controller = AgentController(self.store, self.llm, config, self.memory, self.skills)
        self.evolution = EvolutionService(self.store, self.skills, self.llm, config, runner=self.run_isolated)
        self.jobs: dict[str, asyncio.Task] = {}
        self._worker = None
        self.worker_error = None

    def create_task(self, spec: TaskSpec):
        root = Path(spec.repo_path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("仓库目录不存在")
        spec = spec.model_copy(update={"repo_path": str(root)})
        return self.store.create_task(spec, self.config.snapshot())

    def create_session(self, repo_path: str, title: str | None = None):
        return self.store.create_session(repo_path, title or '')

    async def add_session_message(self, session_id: str, message):
        command = shlex.split(message.acceptance_command) if isinstance(message.acceptance_command, str) else message.acceptance_command
        if not all(isinstance(part, str) for part in command):
            raise ValueError('项目检查命令格式错误')
        # Choosing the execution and persisting the message share a transaction.
        # A client retry cannot create a second task, including across processes.
        with self.store.transaction():
            if message.client_message_id:
                receipt = self.store.get_message_receipt(session_id, message.client_message_id)
                if receipt:
                    return {'session_id': session_id, 'task_id': receipt['task_id'], 'queued': False, 'event_id': receipt['event_id'], 'duplicate': True}
            session_row = self.store.get_session(session_id)['session']
            active_id = session_row['active_task_id']
            active = self.store.get_task(active_id) if active_id else None
            if active:
                task=active
                event=self.store.append_session_event(session_id,'user.message',
                    {'content':message.content,'target_task_id':active_id,'intent':'auto' if message.intent=='new_task' else message.intent},
                    task_id=active_id,client_message_id=message.client_message_id)
            else:
                task=self.create_task(TaskSpec(repo_path=session_row['repo_path'],prompt=message.content,
                    acceptance_command=command,session_id=session_id,repository_id=session_row['repository_id']))
                event=next(e for e in self.store.read_events(task_id=task['task_id']) if e['event_type']=='user.message')
                if message.client_message_id:
                    self.store.link_message_receipt(session_id,message.client_message_id,event['event_id'])
        task_id=task['task_id']
        blocked=task['status']=='needs_review'
        job=self.jobs.get(task_id)
        if not blocked:
            if job and not job.done():
                self.controller.enqueue_user_message(task_id,event)
                if task['status']=='waiting_user':
                    resume_key='resume:'+task_id
                    pending=self.jobs.get(resume_key)
                    if pending is None or pending.done():
                        self.schedule(resume_key,self._continue_after_job(task_id,job))
            else:
                self.schedule(task_id,self.run(task_id,resume=task['status']!='created'))
        return {'session_id':session_id,'task_id':task_id,'queued':not blocked,
                'event_id':event['event_id'],'blocked':blocked,'task':task}

    async def _continue_after_job(self, task_id, previous):
        await asyncio.shield(previous)
        self.schedule(task_id,self.run(task_id,resume=True))

    async def run(self, task_id: str, resume=False):
        current = self.store.get_task(task_id)
        if current['status'] == 'completed':
            raise ValueError('任务已经完成，请创建新任务')
        if not resume and current['status'] != 'created':
            raise ValueError('已有执行记录的任务请使用恢复操作')
        lock_dir = Path(self.config.data_dir) / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        # IDs are internal UUIDs; reject traversal before opening a lock file.
        if "/" in task_id or ".." in task_id:
            raise ValueError("invalid task id")
        repository_lock = 'repo-' + hashlib.sha256(current['repo_path'].encode()).hexdigest()
        with (lock_dir / f"{task_id}.lock").open("w") as lock, (lock_dir / f'{repository_lock}.lock').open('w') as repo_lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(repo_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("任务正在其他进程中运行") from exc
            try:
                result = await (self.controller.resume(task_id) if resume else self.controller.run(task_id))
                if self.config.evolution.enabled and result['status'] in ('completed', 'failed'):
                    self.schedule(f"evolve:{task_id}", self.evolution.on_task_finished(task_id))
                return result
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                fcntl.flock(repo_lock, fcntl.LOCK_UN)

    def schedule(self, key, coroutine):
        if key in self.jobs and not self.jobs[key].done():
            coroutine.close()
            raise ValueError("操作已在运行")
        task = asyncio.create_task(coroutine)
        self.jobs[key] = task
        def done(future):
            if not future.cancelled():
                error = future.exception()
                if error:
                    self.worker_error = str(error)
        task.add_done_callback(done)
        return task

    async def stop_task(self, task_id):
        """Stop a task and wait for cancellation cleanup before acknowledging it."""
        task=self.store.get_task(task_id)
        if task['status'] in ('completed','failed','cancelled','needs_review','interrupted'):
            return task

        runner=self.jobs.get(task_id) or self.controller._active.get(task_id)
        continuation=self.jobs.get('resume:'+task_id)
        active=task_id in self.controller._active
        if continuation and not continuation.done():
            continuation.cancel()

        self.controller.stop(task_id)
        if not active:
            # A scheduled asyncio task may not have entered AgentController.run
            # yet. Cancel it so it cannot race the persisted interrupted state.
            if runner and not runner.done():
                runner.cancel()
        waiting=[job for job in (runner,continuation) if job and not job.done()]
        if waiting:
            await asyncio.gather(*waiting,return_exceptions=True)
        return self.store.get_task(task_id)

    async def start(self):
        async def worker():
            while True:
                try:
                    if self.config.raw_rag.background_indexing and (self.config.raw_rag.enabled or self.config.raw_rag.index_when_disabled):
                        await self.memory.index_pending(limit=50)
                except Exception as exc:
                    self.worker_error = f"index: {exc}"
                await asyncio.sleep(3)
        self._worker = asyncio.create_task(worker())

    async def close(self):
        for key, job in list(self.jobs.items()):
            if not job.done() and not key.startswith(("evolve:", "eval:", "external-audit:")):
                if key.startswith('resume:'):
                    job.cancel()
                else:
                    self.controller.cancel(key)
        pending = [j for j in self.jobs.values() if not j.done()]
        if pending:
            _, remaining = await asyncio.wait(pending, timeout=5)
            for job in remaining:
                job.cancel()
            await asyncio.gather(*remaining, return_exceptions=True)
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        self.store.close()

    async def run_isolated(
        self, spec: dict, snapshot: dict, workspace: str, *,
        llm_factory=None, frozen_memory_collection: str | None = None,
        automatic_memory_query: str | None = None,
    ) -> dict:
        """Each evaluation branch receives a private DB/index/skill namespace.

        ``llm_factory`` is an optional per-run injection seam for controlled
        evaluations. It receives the isolated branch's resolved ``AppConfig``
        and must return a ModelClient-compatible object. Production callers
        omit it and keep the normal configured client.

        A frozen memory collection belongs to the caller and is reused without
        reindexing the branch or writing evaluation events into that collection.
        """
        from evocode.agent import AgentController
        from evocode.memory import MemoryService
        from evocode.persistence import EventStore
        from evocode.skills import SkillRegistry
        branch = Path(self.config.data_dir).resolve() / "evaluations" / str(uuid.uuid4())
        branch.mkdir(parents=True)
        config = AppConfig.model_validate(snapshot.get("config") or self.config.snapshot())
        config.data_dir = str(branch)
        config.evolution.enabled = False
        config.runtime.execution_backend = "docker"
        config.raw_rag.collection = frozen_memory_collection or "eval_" + branch.name.replace("-", "")
        if frozen_memory_collection:
            config.raw_rag.background_indexing = False
        source = snapshot.get("source_data_dir")
        if source and (Path(source) / "evocode.db").exists():
            with sqlite3.connect(Path(source) / "evocode.db") as src, sqlite3.connect(config.sqlite_path) as dst:
                src.backup(dst)
            for directory in ("artifacts", "skills", "skill_versions"):
                if (Path(source) / directory).exists():
                    shutil.copytree(Path(source) / directory, branch / directory, dirs_exist_ok=True)
        store = EventStore(config.sqlite_path, branch / "artifacts")
        if source:
            old_root = Path(snapshot.get('frozen_source_root', source)).resolve()
            with store.transaction() as conn:
                for artifact in conn.execute('SELECT artifact_id,storage_path FROM artifacts').fetchall():
                    old = Path(artifact['storage_path'])
                    if old.is_relative_to(old_root):
                        conn.execute('UPDATE artifacts SET storage_path=? WHERE artifact_id=?', (str(branch / old.relative_to(old_root)),artifact['artifact_id']))
        registry = SkillRegistry(store, branch)
        repository_id = spec.get('repository_id') or hashlib.sha256(str(Path(snapshot.get('source_root',spec['repo_path'])).resolve()).encode()).hexdigest()[:24]
        # Seed branch versions directly as a snapshot import, never publish into live registry.
        with store.transaction() as conn:
            conn.execute("UPDATE skills SET active_version_id=NULL")
        for version in snapshot.get("skill_versions", []):
            if not isinstance(version, dict) or not version.get("content"):
                continue
            candidate = registry.create_candidate(name=version.get("name", version.get("skill_id", "evaluation-skill")), content=version["content"], level=version.get("level", "project"), scope_id=version.get('scope_id') or repository_id, metadata={**version.get('metadata',{}), "snapshot_import": True})
            with store.transaction() as conn:
                conn.execute("UPDATE skill_versions SET status='active' WHERE version_id=?", (candidate["version_id"],))
                conn.execute("UPDATE skills SET status='active',active_version_id=? WHERE skill_id=?", (candidate["version_id"], candidate["skill_id"]))
        memory = MemoryService(store, config)
        if config.raw_rag.enabled and config.raw_rag.embedding_model and config.raw_rag.background_indexing:
            await memory.rebuild_index()
        llm = llm_factory(config) if llm_factory is not None else ModelClient(config)
        controller = AgentController(
            store, llm, config, memory, registry,
            automatic_memory_query=automatic_memory_query,
        )
        acceptance_command = spec.get("acceptance_command", [])
        task_spec = TaskSpec(
            repo_path=workspace,
            prompt=spec["prompt"],
            acceptance_command=[] if snapshot.get("oracle_only_acceptance") else acceptance_command,
            repository_id=repository_id,
        )
        task = store.create_task(task_spec, config.snapshot())
        started = time.monotonic()
        try:
            result = await controller.run(task["task_id"])
            events = store.read_events(task_id=task["task_id"])
            coder_skill_injection_evidence = _verified_coder_skill_injections(
                store, events, registry,
            )
            token_totals = [e["payload"].get("provider_total_tokens") for e in events
                            if e["event_type"] == "model.usage"]
            usage_complete = bool(token_totals) and all(isinstance(value, int) for value in token_totals)
            protected = snapshot.get('acceptance_files') or {}
            acceptance = await run_protected_acceptance(
                config, workspace, branch / "oracle_workspace", protected, acceptance_command,
            )
            if protected and acceptance_command:
                store.save_artifact(acceptance["acceptance_output"], kind="protected_acceptance")
            return {
                **acceptance,
                "tool_calls": sum(e["event_type"] == "tool.started" for e in events),
                "tokens": sum(token_totals) if usage_complete else None,
                "model_calls": len(token_totals),
                "tokens_complete": usage_complete,
                "duration_seconds": time.monotonic() - started,
                "task_id": task["task_id"],
                "artifacts": str(branch),
                "coder_skill_injection_evidence": coder_skill_injection_evidence,
                "result": result,
            }
        finally:
            store.close()


async def run_protected_acceptance(config, workspace, oracle, protected, acceptance_command) -> dict:
    """Run trusted assertions against a copy of Agent output in read-only Docker."""
    if not protected or not acceptance_command:
        return {
            "passed": False,
            "acceptance_protected": False,
            "acceptance_file_sha256": {},
            "acceptance_output": "No protected acceptance files",
        }

    shutil.copytree(workspace, oracle, symlinks=True)
    oracle_root = oracle.resolve()
    file_hashes = {}
    for relative, content in protected.items():
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("Acceptance path escapes workspace")
        destination = oracle_root / relative_path
        if destination.resolve() != destination:
            raise ValueError("Acceptance path was redirected by a symlink")
        destination.parent.mkdir(parents=True, exist_ok=True)
        content_bytes = content.encode("utf-8")
        destination.write_bytes(content_bytes)
        file_hashes[relative] = hashlib.sha256(content_bytes).hexdigest()

    name = "evocode-oracle-" + uuid.uuid4().hex
    command = [
        "docker", "run", "--rm", "--name", name, "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128",
        "--memory", "2g", "--cpus", "2", "--tmpfs", "/tmp:rw,nosuid,size=128m",
        "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPYCACHEPREFIX=/tmp/evocode-pycache",
        "--mount", f"type=bind,src={oracle_root},dst=/workspace,readonly",
        "-w", "/workspace", config.runtime.docker_image,
        *acceptance_command,
    ]
    proc = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), config.evolution.timeout)
        return {
            "passed": proc.returncode == 0,
            "acceptance_protected": proc.returncode not in (125, 126, 127),
            "acceptance_file_sha256": file_hashes,
            "acceptance_output": stdout.decode(errors="replace")[-24000:],
        }
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
            cleanup = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", name, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await cleanup.wait()
