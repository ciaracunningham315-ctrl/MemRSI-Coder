"""Prior execution failures reach a fresh session turn as bounded facts."""
import asyncio
import json

from evocode.app import Application
from evocode.config import AppConfig
from evocode.contracts import LLMResponse, SessionMessageSpec, TaskSpec


def configuration(tmp_path):
    cfg = AppConfig(data_dir=str(tmp_path / 'data'))
    cfg.raw_rag.enabled = False
    cfg.raw_rag.background_indexing = False
    cfg.skills.enabled = False
    cfg.evolution.enabled = False
    cfg.runtime.execution_backend = 'host'
    return cfg


class SessionModel:
    def __init__(self):
        self.calls = 0
        self.contexts = []

    async def complete(self, messages, *, role, **kwargs):
        assert role == 'planner'
        self.calls += 1
        self.contexts.append(messages)
        if self.calls == 2:
            raise RuntimeError('Context budget exceeded while assembling task context')
        answer = '第一个任务已完成。' if self.calls == 1 else '根据记录，前一个任务因上下文预算超限而失败。'
        context = json.loads(messages[-1]['content'].split('\n', 1)[1])
        return LLMResponse(content=json.dumps({
            'action': 'respond', 'answer': answer,
            'requirements': {'source_user_seq': context['consumed_user_seq'], 'goal': 'Answer the question'},
        }, ensure_ascii=False))


async def settle(app, task_id):
    return await asyncio.wait_for(app.jobs[task_id], 3)


async def test_new_followup_sees_factual_failure_without_internal_history(tmp_path):
    model = SessionModel()
    app = Application(configuration(tmp_path), model)
    session_id = app.create_session(str(tmp_path))['session']['session_id']
    try:
        first = await app.add_session_message(session_id, SessionMessageSpec(content='完成一个简单任务'))
        assert (await settle(app, first['task_id']))['status'] == 'completed'

        failed = await app.add_session_message(session_id, SessionMessageSpec(content='创建项目文件'))
        failed_result = await settle(app, failed['task_id'])
        assert failed_result['status'] == 'failed'

        followup = await app.add_session_message(session_id, SessionMessageSpec(content='刚才为什么失败？'))
        assert followup['task_id'] != failed['task_id']
        assert (await settle(app, followup['task_id']))['status'] == 'completed'

        prompt = next(item['content'] for item in model.contexts[2]
                      if item.get('content', '').startswith('Current task context'))
        context = json.loads(prompt.split('\n', 1)[1])
        working = context['working_state']
        assert working['last_task_id'] == first['task_id']
        diagnostic = working['last_failed_task']
        assert diagnostic == {
            'task_id': failed['task_id'],
            'status': 'failed',
            'error': 'RuntimeError: Context budget exceeded while assembling task context',
            'changed_files': [],
            'verification': {},
        }
        assert 'cannot write' not in json.dumps(context, ensure_ascii=False).lower()
        assert 'coder_messages' not in working
        assert 'events' not in working
    finally:
        await app.close()


async def test_historical_failed_task_is_derived_without_migration_write(tmp_path):
    model = SessionModel()
    app = Application(configuration(tmp_path), model)
    session = app.create_session(str(tmp_path))['session']
    try:
        legacy = app.store.create_task(TaskSpec(
            repo_path=str(tmp_path), prompt='创建旧任务', session_id=session['session_id'],
            repository_id=session['repository_id'],
        ), app.config.snapshot())
        app.store.append_event(legacy['task_id'], 'code.patch', {
            'patch': 'diff --git a/main.py b/main.py', 'file_paths': ['main.py'],
        })
        legacy_state = legacy['state']
        legacy_state['error'] = 'Context budget exceeded'
        legacy_state['last_coder_report'] = {
            'step_id': 's1', 'outcome': 'failed', 'summary': 'Earlier edit attempt', 'changed_files': [],
        }
        app.store.update_task(legacy['task_id'], status='failed', state=legacy_state)

        followup = await app.add_session_message(session['session_id'], SessionMessageSpec(content='旧任务为什么失败？'))
        assert followup['task_id'] != legacy['task_id']
        assert (await settle(app, followup['task_id']))['status'] == 'completed'
        prompt = next(item['content'] for item in model.contexts[0]
                      if item.get('content', '').startswith('Current task context'))
        context = json.loads(prompt.split('\n', 1)[1])
        diagnostic = context['working_state']['last_failed_task']
        assert diagnostic == {
            'task_id': legacy['task_id'],
            'status': 'failed',
            'error': 'Context budget exceeded',
            'changed_files': ['main.py'],
            'verification': {},
        }
        assert len(json.dumps(diagnostic, ensure_ascii=False)) < 2000

        persisted = app.store.connection.execute(
            'SELECT working_state_json FROM sessions WHERE session_id=?', (session['session_id'],)
        ).fetchone()[0]
        assert 'last_failed_task' not in json.loads(persisted)
    finally:
        await app.close()
