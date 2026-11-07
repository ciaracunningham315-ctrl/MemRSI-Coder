from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from evocode.api import create_app
from evocode.app import Application
from evocode.config import AppConfig
from evocode.contracts import TaskSpec


def test_evolution_api_batch_and_idempotency(tmp_path):
    config = AppConfig(data_dir=str(tmp_path / 'state'))
    config.evolution.enabled = False
    config.raw_rag.background_indexing = False
    service = Application(config)
    tasks = [service.create_task(TaskSpec(repo_path=str(tmp_path), prompt=str(i), repository_id='repo')) for i in range(2)]
    for task in tasks:
        service.store.update_task(task['task_id'], status='completed')
    ids = [task['task_id'] for task in tasks]
    with TestClient(create_app(application=service)) as client:
        request = {'task_id': ids[-1], 'trace_task_ids': ids, 'idempotency_key': 'batch-one'}
        first = client.post('/api/evolution', json=request)
        assert first.status_code == 200
        assert first.json()['payload']['trace_task_ids'] == ids
        retry = client.post('/api/evolution', json=request)
        assert retry.json()['run_id'] == first.json()['run_id']
        assert len(client.get('/api/evolution').json()) == 1
        assert client.post('/api/evolution', json={**request, 'trace_task_ids': [ids[-1]]}).status_code == 400
        assert client.post('/api/evolution', json={'trace_task_ids': []}).status_code == 400
        assert client.post('/api/evolution', json={'trace_task_ids': [ids[0], ids[0]]}).status_code == 400


def test_meta_api_forwards_frozen_task_run_order(tmp_path):
    config = AppConfig(data_dir=str(tmp_path / 'state'))
    service = Application(config)
    service.evolution.trigger = AsyncMock(return_value={'run_id': 'meta-run'})
    ordered_runs = ['task-run-stage1-repo_api', 'task-run-stage1-repo_cli']
    with TestClient(create_app(application=service)) as client:
        response = client.post('/api/evolution', json={
            'kind': 'meta', 'trace_task_ids': ['task-a', 'task-b'],
            'meta_history_run_ids': ordered_runs, 'idempotency_key': 'meta-order',
        })
        assert response.status_code == 200
        assert service.evolution.trigger.call_args.kwargs['meta_history_run_ids'] == ordered_runs


def test_evolution_api_rejects_inflight_and_cross_repository_traces(tmp_path):
    config = AppConfig(data_dir=str(tmp_path / 'state'))
    config.evolution.enabled = False
    config.raw_rag.background_indexing = False
    service = Application(config)
    first = service.create_task(TaskSpec(repo_path=str(tmp_path), prompt='one', repository_id='a'))
    second = service.create_task(TaskSpec(repo_path=str(tmp_path), prompt='two', repository_id='b'))
    with TestClient(create_app(application=service)) as client:
        assert client.post('/api/evolution', json={'trace_task_ids': [first['task_id']]}).status_code == 400
        service.store.update_task(first['task_id'], status='completed')
        service.store.update_task(second['task_id'], status='completed')
        assert client.post('/api/evolution', json={'trace_task_ids': [first['task_id'], second['task_id']]}).status_code == 400


def test_evaluation_api_forwards_frozen_collection(tmp_path):
    config = AppConfig(data_dir=str(tmp_path / 'state'))
    config.raw_rag.background_indexing = False
    service = Application(config)
    service.evolution.evaluate = AsyncMock(return_value={})
    with TestClient(create_app(application=service)) as client:
        response = client.post('/api/evolution/run/evaluate', json={
            'task_specs': [{'prompt': 'check'}], 'frozen_memory_collection': 'stage_frozen',
        })
        assert response.status_code == 200
        assert service.evolution.evaluate.call_args.kwargs['frozen_memory_collection'] == 'stage_frozen'
        assert client.post('/api/evolution/other/evaluate', json={
            'task_specs': [], 'frozen_memory_collection': '../invalid',
        }).status_code == 400
