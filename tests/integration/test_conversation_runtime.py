"""Session, persistence and runtime acceptance without external model calls."""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from evocode.app import Application
from evocode.config import AppConfig
from evocode.contracts import LLMResponse, SessionMessageSpec, TaskSpec
from evocode.persistence import EventStore


def configuration(tmp_path):
    cfg=AppConfig(data_dir=str(tmp_path/'data'))
    cfg.raw_rag.enabled=False
    cfg.raw_rag.background_indexing=False
    cfg.skills.enabled=False
    cfg.evolution.enabled=False
    cfg.runtime.execution_backend='host'
    return cfg


class Responses:
    def __init__(self, decisions):
        self.decisions=iter(decisions)
        self.contexts=[]

    async def complete(self,messages,*,role,**kwargs):
        assert role=='planner', 'These requests require no coding or tools'
        self.contexts.append(messages)
        return self.decision(messages, next(self.decisions))

    @staticmethod
    def decision(messages, decision):
        context = json.loads(messages[-1]['content'].split('\n', 1)[1])
        current = context['current_requirements']
        return LLMResponse(content=json.dumps({**decision, 'requirements': {
            'expected_revision': current['revision'] if current else 0,
            'source_user_seq': context['consumed_user_seq'],
            'goal': context['latest_user_requirement'],
        }}, ensure_ascii=False))


async def settle(app,task_id):
    return await asyncio.wait_for(app.jobs[task_id],3)


async def test_automatic_turns_and_durable_session_history(tmp_path):
    model=Responses([{'action':'respond','answer':'你好！'}, {'action':'respond','answer':'你好！'}, {'action':'respond','answer':'你发送了两次独立的“你好”。'}])
    app=Application(configuration(tmp_path),model)
    sid=app.create_session(str(tmp_path))['session']['session_id']
    try:
        ids=[]
        for text in ['你好','你好','我发过几次你好？']:
            receipt=await app.add_session_message(sid,SessionMessageSpec(content=text))
            ids.append(receipt['task_id'])
            assert (await settle(app,receipt['task_id']))['status']=='completed'
        assert len(set(ids))==3
        session=app.store.get_session(sid)
        assert [m['content'] for m in session['messages'] if m['role']=='user']==['你好','你好','我发过几次你好？']
        assert len([m for m in session['messages'] if m['kind']=='final'])==3
        assert session['session']['active_task_id'] is None
        assert '你好！' in json.dumps(model.contexts[-1],ensure_ascii=False)
        assert session['session']['working_state']['last_task_id']==ids[-1]
    finally:
        await app.close()


async def test_ask_user_resumes_same_task_after_restart(tmp_path):
    cfg=configuration(tmp_path)
    app=Application(cfg,Responses([{'action':'ask_user','question':'使用哪个目录？'}]))
    sid=app.create_session(str(tmp_path))['session']['session_id']
    receipt=await app.add_session_message(sid,SessionMessageSpec(content='检查项目'))
    tid=receipt['task_id']
    assert (await settle(app,tid))['status']=='waiting_user'
    await app.close()
    restarted=Application(cfg,Responses([{'action':'respond','answer':'已确认使用当前目录。'}]))
    try:
        followup=await restarted.add_session_message(sid,SessionMessageSpec(content='就用当前目录'))
        assert followup['task_id']==tid
        assert (await settle(restarted,tid))['status']=='completed'
        messages=restarted.store.get_session(sid)['messages']
        assert [m['kind'] for m in messages]==['user','question','user','final']
    finally:
        await restarted.close()


async def test_active_input_supersedes_stale_planner_reply_and_retry_dedupes(tmp_path):
    entered=asyncio.Event(); release=asyncio.Event()
    class Delayed(Responses):
        async def complete(self,messages,*,role,**kwargs):
            if not self.contexts:
                self.contexts.append(messages);entered.set();await release.wait()
                return self.decision(messages, {'action': 'respond', 'answer': 'stale'})
            self.contexts.append(messages)
            return self.decision(messages, {'action': 'respond', 'answer': 'followed latest instruction'})
    app=Application(configuration(tmp_path),Delayed([]))
    sid=app.create_session(str(tmp_path))['session']['session_id']
    try:
        first=await app.add_session_message(sid,SessionMessageSpec(content='先处理这个',client_message_id='first'))
        await asyncio.wait_for(entered.wait(),2)
        second=await app.add_session_message(sid,SessionMessageSpec(content='改一下方向',client_message_id='second'))
        retry=await app.add_session_message(sid,SessionMessageSpec(content='改一下方向',client_message_id='second'))
        third=await app.add_session_message(sid,SessionMessageSpec(content='以最后这条要求为准',client_message_id='third'))
        assert first['task_id']==second['task_id']==retry['task_id']==third['task_id']
        assert retry['duplicate'] is True
        release.set()
        result=await settle(app,first['task_id'])
        assert result['status']=='completed',result
        messages=app.store.get_session(sid)['messages']
        assert [m['content'] for m in messages]==['先处理这个','改一下方向','以最后这条要求为准','followed latest instruction']
        assert result['state']['current_requirements']['goal']=='以最后这条要求为准'
        assert len(app.store.list_tasks())==1
    finally:
        release.set()
        await app.close()


def test_final_commit_is_atomic_idempotent_and_checks_input_cursor(tmp_path):
    cfg=configuration(tmp_path);store=EventStore(cfg.sqlite_path)
    try:
        task=store.create_task(TaskSpec(repo_path=str(tmp_path),prompt='first'),cfg.snapshot())
        tid=task['task_id'];state=task['state'];state['last_user_event_seq']=1
        appended=store.append_session_event(task['session_id'],'user.message',{'content':'new'},task_id=tid)
        assert store.publish_final(tid,'stale',state) is None
        assert not [m for m in store.read_conversation_messages(task['session_id']) if m['kind']=='final']
        state['last_user_event_seq']=appended['seq']
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.publish_final(tid,'rollback',dict(state))
                raise RuntimeError('simulate aborted commit')
        assert store.get_task(tid)['status']=='created'
        store.publish_final(tid,'accepted',state)
        store.publish_final(tid,'duplicate',state)
        assert [m['content'] for m in store.read_conversation_messages(task['session_id']) if m['kind']=='final']==['accepted']
        assert store.get_task(tid)['state']['final_message_id']
    finally:
        store.close()


def test_legacy_database_migration_preserves_only_public_messages(tmp_path):
    cfg=configuration(tmp_path);cfg.sqlite_path.parent.mkdir()
    db=sqlite3.connect(cfg.sqlite_path)
    migrations=Path(__file__).parents[2]/'src/evocode/persistence/migrations'
    db.execute('CREATE TABLE schema_migrations(name TEXT PRIMARY KEY)')
    for path in sorted(migrations.glob('*.sql')):
        if path.name.startswith('004'):
            break
        db.executescript(path.read_text());db.execute('INSERT INTO schema_migrations VALUES(?)',(path.name,))
    db.execute("INSERT INTO repositories VALUES('r',?,'{}')",(str(tmp_path),))
    db.execute("INSERT INTO sessions VALUES('s','r','2026-01-01','old','2026-01-01')")
    db.execute("INSERT INTO tasks VALUES('t','s','r',?,'你好','completed',?,'{}','2026-01-01','2026-01-01')",(str(tmp_path),json.dumps({'final_answer':'real final'})))
    for seq,kind,payload in [(1,'user.message',{'content':'你好'}),(2,'coder.message',{'message':{'content':'internal duplicate'}})]:
        db.execute("INSERT INTO events VALUES(?,'s',?,'t','r',?,?,'2026-01-01')",(str(seq),seq,kind,json.dumps(payload)))
    db.commit();db.close()
    store=EventStore(cfg.sqlite_path)
    try:
        assert [m['content'] for m in store.get_session('s')['messages']]==['你好','real final']
        assert len(store.read_events(task_id='t'))==2
        assert store.get_session('s')['session']['revision']==1
    finally:
        store.close()
