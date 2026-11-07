"""Cross-module memory and Skill baselines over real SQLite state."""
import asyncio
import json
from uuid import uuid4

import pytest

from evocode.app import Application
from evocode.config import AppConfig
from evocode.context import ContextAssembler
from evocode.contracts import LLMResponse, SessionMessageSpec, TaskSpec


def _approve(registry, version):
    evaluation_id = str(uuid4())
    with registry.store.transaction() as db:
        db.execute(
            "INSERT INTO evaluations VALUES (?,?,?,?)",
            (evaluation_id, None, json.dumps({
                "evaluation_id": evaluation_id,
                "candidate_version_id": version["version_id"],
                "passed": True,
                "status": "approved",
            }), "now"),
        )
    return {"evaluation_id": evaluation_id}


class BaselineModel:
    """Small Planner/Coder protocol substitute; no provider calls."""

    def __init__(self):
        self.planner_calls = 0
        self.coder_calls = []

    async def complete(self, messages, *, role, tools=None, **kwargs):
        if role == "planner":
            self.planner_calls += 1
            context_message = next(
                message["content"] for message in messages
                if message.get("content", "").startswith(
                    "Current task context (authoritative current requirements):"
                )
            )
            context = json.loads(context_message.split("\n", 1)[1])
            current = context["current_requirements"]
            requirements = {
                "expected_revision": current["revision"] if current else 0,
                "source_user_seq": context["consumed_user_seq"],
                "goal": context["latest_user_requirement"],
            }
            if self.planner_calls == 1:
                decision = {"action": "ask_user", "question": "继续执行吗？"}
            elif self.planner_calls == 2:
                decision = {
                    "action": "delegate",
                    "assignment": {
                        "step_id": "use-pinned-skill",
                        "goal": "Use the applicable project guidance.",
                    },
                }
            else:
                decision = {"action": "respond", "answer": "Task completed."}
            return LLMResponse(content=json.dumps({
                **decision, "requirements": requirements,
            }, ensure_ascii=False))

        assert role == "coder"
        self.coder_calls.append({"messages": messages, "tools": tools})
        return LLMResponse(content=json.dumps({
            "step_id": "use-pinned-skill",
            "outcome": "completed",
            "summary": "Used the pinned project guidance.",
        }))


@pytest.mark.asyncio
async def test_cross_session_retrieval_is_auditable_and_task_pins_skill_with_rag_off(
    tmp_path, monkeypatch,
):
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.raw_rag.enabled = False
    config.raw_rag.index_when_disabled = True
    config.raw_rag.background_indexing = False
    config.skills.enabled = True
    config.evolution.enabled = False
    config.runtime.execution_backend = "host"
    model = BaselineModel()
    app = Application(config, model)
    repository = tmp_path / "repo"
    repository.mkdir()
    session_a = app.create_session(str(repository))["session"]["session_id"]
    session_b = app.create_session(str(repository))["session"]["session_id"]
    repository_id = app.store.get_session(session_a)["session"]["repository_id"]

    try:
        # Seed one real event in Session A and prove Session B can retrieve it
        # within the same repository, with the source session still attached.
        source_task = app.store.create_task(TaskSpec(
            repo_path=str(repository), prompt="historical source",
            session_id=session_a, repository_id=repository_id,
        ), config.snapshot())
        marker = "historicalregressionmarker829"
        source_event = app.store.append_event(source_task["task_id"], "test.result", {
            "output": f"Observed {marker} in this earlier execution.",
        })

        async def no_vector_writes(*args, **kwargs):
            return None

        async def no_dense_match(*args, **kwargs):
            return []

        monkeypatch.setattr(app.memory, "_upsert", no_vector_writes)
        monkeypatch.setattr(app.memory, "_dense", no_dense_match)
        indexed = await app.memory.index_pending()
        assert indexed["indexed"] >= 1

        task = app.store.create_task(TaskSpec(
            repo_path=str(repository), prompt="probe the applicable project guidance",
            session_id=session_b, repository_id=repository_id,
        ), config.snapshot())
        search_snapshot = {**task, "config": json.loads(json.dumps(task["config"]))}
        search_snapshot["config"]["raw_rag"]["enabled"] = True
        evidence = await app.memory.search(marker, search_snapshot)
        match = next(item for item in evidence.evidence if item.event_id == source_event["event_id"])
        assert match.session_id == session_a
        assert match.task_id == source_task["task_id"]
        assert marker in match.text

        # The task's immutable config is Raw-RAG off. Even if a stale evidence
        # object is passed across the controller boundary, ContextAssembler
        # must strip it before persisting or sending the model input.
        retrieval_calls = []

        async def forbidden_search(*args, **kwargs):
            retrieval_calls.append(args)
            raise AssertionError("Raw-RAG search must not run for this task")

        monkeypatch.setattr(app.memory, "search", forbidden_search)
        original_build = ContextAssembler.build

        async def stale_evidence_build(assembler, current_task, role, messages,
                                       evidence=None, skills=None, **options):
            injected = [{"event_id": source_event["event_id"], "text": marker}]
            return await original_build(
                assembler, current_task, role, messages,
                evidence=injected, skills=skills, **options,
            )

        monkeypatch.setattr(ContextAssembler, "build", stale_evidence_build)

        skill_v1 = app.skills.create_candidate(
            "probe guidance", "PINNED_SKILL_V1", scope_id=repository_id,
            metadata={"trigger_terms": ["probe"]},
        )
        app.skills.publish_version(skill_v1["version_id"], _approve(app.skills, skill_v1))

        first = await app.controller.run(task["task_id"])
        assert first["status"] == "waiting_user"
        pinned = first["state"]["skill_snapshot"][skill_v1["skill_id"]]
        assert pinned == skill_v1["version_id"]

        # Publish a new active version between task turns. The already-started
        # task must continue using its captured version after the user resumes it.
        skill_v2 = app.skills.create_candidate(
            "probe guidance", "ACTIVE_SKILL_V2", scope_id=repository_id,
            skill_id=skill_v1["skill_id"], metadata={"trigger_terms": ["probe"]},
        )
        app.skills.publish_version(skill_v2["version_id"], _approve(app.skills, skill_v2))

        resumed = await app.add_session_message(
            session_b, SessionMessageSpec(content="继续执行"),
        )
        assert resumed["task_id"] == task["task_id"]
        result = await asyncio.wait_for(app.jobs[task["task_id"]], 3)
        assert result["status"] == "completed"

        coder_call = model.coder_calls[0]
        rendered = json.dumps(coder_call["messages"], ensure_ascii=False)
        assert "PINNED_SKILL_V1" in rendered
        assert "ACTIVE_SKILL_V2" not in rendered
        assert marker not in rendered
        assert all(tool["function"]["name"] != "memory_search" for tool in coder_call["tools"])
        assert retrieval_calls == []

        assembled = [event for event in app.store.read_events(task_id=task["task_id"])
                     if event["event_type"] == "context.assembled"]
        coder_context = next(event for event in assembled if event["payload"]["role"] == "coder")
        assert coder_context["payload"]["evidence_count"] == 0
        assert coder_context["payload"]["node_context"]["evidence_refs"] == []
        assert coder_context["payload"]["skill_count"] == 1
        assert app.skills.snapshot(repository_id)[skill_v1["skill_id"]] == skill_v2["version_id"]
    finally:
        await app.close()

@pytest.mark.asyncio
async def test_frozen_history_filters_fts_dense_and_neighbor_expansion(tmp_path, monkeypatch):
    config = AppConfig(data_dir=str(tmp_path / 'data'))
    app = Application(config)
    repo = tmp_path / 'repo'
    repo.mkdir()
    spec = lambda prompt: TaskSpec(repo_path=str(repo), prompt=prompt, repository_id='same-repo')
    old = app.store.create_task(spec('old'), config.snapshot())
    current = app.store.create_task(spec('current'), config.snapshot())
    other = app.store.create_task(spec('other'), config.snapshot())
    marker = 'frozenhistoryprobe'
    old_event = app.store.append_event(old['task_id'], 'tool.completed', {'output': marker})
    late_event = app.store.append_event(old['task_id'], 'tool.completed', {'output': 'late-secret'})
    own_event = app.store.append_event(current['task_id'], 'tool.completed', {'output': marker})
    other_event = app.store.append_event(other['task_id'], 'tool.completed', {'output': marker})

    async def upsert(*_args):
        pass
    monkeypatch.setattr(app.memory, '_upsert', upsert)
    await app.memory.index_pending(500)
    with app.store.transaction() as db:
        dense_ids = [r['chunk_id'] for r in db.execute('SELECT chunk_id FROM memory_chunks WHERE task_id IN (?, ?)',
                                                      (current['task_id'], other['task_id']))]
    async def dense(*_args):
        return dense_ids
    monkeypatch.setattr(app.memory, '_dense', dense)
    snapshot = dict(current)
    snapshot['config'] = config.snapshot()
    snapshot['config']['raw_rag']['history_allowed_task_ids'] = [old['task_id']]
    snapshot['config']['raw_rag']['history_allowed_event_ids'] = [old_event['event_id']]
    try:
        result = await app.memory.search(marker, snapshot)
        assert [item.event_id for item in result.evidence] == [old_event['event_id']]
        assert 'late-secret' not in result.evidence[0].text
        assert late_event['event_id'] not in result.evidence[0].metadata['related_event_ids']
        assert own_event['event_id'] not in [item.event_id for item in result.evidence]
        assert other_event['event_id'] not in [item.event_id for item in result.evidence]
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_incremental_index_only_processes_allowed_event_ids(tmp_path, monkeypatch):
    config = AppConfig(data_dir=str(tmp_path / 'data'))
    app = Application(config)
    repo = tmp_path / 'repo'
    repo.mkdir()
    task = app.store.create_task(TaskSpec(repo_path=str(repo), prompt='task', repository_id='repo'), config.snapshot())
    allowed = app.store.append_event(task['task_id'], 'tool.completed', {'output': 'included'})
    excluded = app.store.append_event(task['task_id'], 'tool.completed', {'output': 'excluded'})
    seen = []
    async def upsert(chunk, _cfg):
        seen.append(chunk['metadata']['event_id'])
    monkeypatch.setattr(app.memory, '_upsert', upsert)
    try:
        result = await app.memory.index_pending(100, allowed_event_ids=[allowed['event_id']])
        assert result['indexed'] == 1
        assert seen == [allowed['event_id']]
        with app.store.transaction() as db:
            assert db.execute('SELECT count(*) FROM memory_chunks WHERE metadata_json LIKE ?',
                              (f'%{excluded["event_id"]}%',)).fetchone()[0] == 0
    finally:
        await app.close()

@pytest.mark.asyncio
async def test_incremental_index_defers_episode_until_all_events_allowed(tmp_path, monkeypatch):
    config = AppConfig(data_dir=str(tmp_path / 'data'))
    app = Application(config)
    repo = tmp_path / 'repo'
    repo.mkdir()
    task = app.store.create_task(TaskSpec(repo_path=str(repo), prompt='task', repository_id='repo'), config.snapshot())
    anchor = app.store.append_event(task['task_id'], 'test.result', {'output': 'anchor'})
    later = app.store.append_event(task['task_id'], 'tool.completed', {'output': 'later'})
    original = app.memory._make_chunks
    def with_episode(event):
        yield from original(event)
        if event['event_id'] == anchor['event_id']:
            yield {'chunk_id': str(uuid4()), 'task_id': task['task_id'], 'session_id': task['session_id'],
                   'repository_id': 'repo', 'text': 'episode',
                   'metadata': {'event_id': anchor['event_id'], 'event_ids': [anchor['event_id'], later['event_id']]}}
    async def upsert(*_args):
        pass
    monkeypatch.setattr(app.memory, '_make_chunks', with_episode)
    monkeypatch.setattr(app.memory, '_upsert', upsert)
    try:
        result = await app.memory.index_pending(100, allowed_event_ids=[anchor['event_id']])
        assert result['deferred'] == 1 and result['indexed'] == 0
        result = await app.memory.index_pending(100, allowed_event_ids=[anchor['event_id'], later['event_id']])
        assert result['deferred'] == 0 and result['indexed'] == 2
    finally:
        await app.close()
