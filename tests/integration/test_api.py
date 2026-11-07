import hashlib
import json
import time
from fastapi.testclient import TestClient
from evocode.api import create_app
from evocode.app import Application
from evocode.config import AppConfig
from evocode.contracts import LLMResponse
from evocode.evolution.service import DEFAULT_META

class Model:
    async def complete(self, messages, *, role='coder', **kwargs):
        if role == 'planner':
            context = json.loads(messages[-1]['content'].split('\n', 1)[1])
            return LLMResponse(content=json.dumps({
                'action': 'respond', 'answer': 'No code changes required.',
                'requirements': {'source_user_seq': context['consumed_user_seq'], 'goal': 'Inspect'},
            }))
        raise AssertionError('Conversational response should not call Coder')

def test_api_task_persistence_and_settings(tmp_path):
    cfg=AppConfig(data_dir=str(tmp_path/'data'))
    cfg.raw_rag.enabled=False;cfg.raw_rag.index_when_disabled=False
    cfg.evolution.enabled=False
    svc=Application(cfg,Model())
    with TestClient(create_app(application=svc)) as client:
        assert client.get('/api/tasks').json()==[]
        r=client.post('/api/tasks',json={'repo_path':str(tmp_path),'prompt':'inspect'})
        assert r.status_code==200
        id=r.json()['task_id']
        for _ in range(30):
            task=client.get('/api/tasks/'+id).json()
            if task['status'] in ('completed','failed'): break
            time.sleep(.02)
        assert task['status']=='completed',task
        assert client.post('/api/tasks/'+id+'/cancel').status_code==409
        events=client.get('/api/tasks/'+id+'/events').json()
        assert any(e['event_type']=='planner.decision' for e in events)
        assert client.get('/api/skills/versions').json()==[]
        assert client.get('/api/evolution/active-meta').json() == {
            'source': 'builtin', 'version_id': None,
            'content_sha256': hashlib.sha256(DEFAULT_META.encode()).hexdigest(),
        }
        assert client.get('/api/tasks/missing').status_code==404
        assert not any('API_KEY=' in str(e) for e in events)

async def test_background_indexing_can_be_disabled_without_paid_calls(tmp_path, monkeypatch):
    import asyncio
    cfg=AppConfig(data_dir=str(tmp_path/'data'))
    cfg.raw_rag.enabled=True
    cfg.raw_rag.background_indexing=False
    svc=Application(cfg,Model())
    async def forbidden(*args,**kwargs):raise AssertionError('Unexpected indexing request')
    monkeypatch.setattr(svc.memory,'index_pending',forbidden)
    await svc.start()
    await asyncio.sleep(.01)
    assert svc.worker_error is None
    await svc.close()
