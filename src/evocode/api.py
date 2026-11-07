import asyncio
import json
import os
import platform
import re
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from evocode.app import Application
from evocode.config import AppConfig, load_config
from evocode.contracts import SessionMessageSpec, SessionSpec, TaskSpec
from evocode.llm import doctor


def create_app(config=None, application=None):
    service = application or Application(config or load_config())
    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        yield
        await service.close()
    api = FastAPI(title='EvoCode', lifespan=lifespan)
    api.state.service = service
    @api.exception_handler(ValueError)
    async def invalid(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': str(exc)}, status_code=400)
    @api.exception_handler(KeyError)
    async def missing(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail': str(exc)}, status_code=404)
    @api.get('/api/health')
    def health(): return {'status':'ok', 'worker_error': service.worker_error}
    @api.get('/api/jobs')
    async def jobs():
        return {'active': sorted(key for key, job in service.jobs.items() if not job.done()),
                'worker_error': service.worker_error}
    @api.get('/api/doctor')
    async def check(): return await doctor(service.config)
    @api.get('/api/tasks')
    def tasks(): return service.store.list_tasks()
    @api.get('/api/sessions')
    def sessions(): return service.store.list_sessions()
    @api.post('/api/sessions')
    def create_session(spec: SessionSpec): return service.create_session(spec.repo_path, spec.title)
    @api.get('/api/sessions/{session_id}')
    def session(session_id: str): return service.store.get_session(session_id)
    @api.get('/api/sessions/{session_id}/messages')
    def session_messages(session_id: str, after_seq: int=0, before_seq: int | None=None, limit: int=100):
        service.store.get_session(session_id)
        if limit < 1 or limit > 500:
            raise ValueError('每次可读取 1 到 500 条消息')
        return service.store.read_conversation_messages(session_id,after_seq,before_seq,limit)
    @api.post('/api/sessions/{session_id}/messages')
    async def session_message(session_id: str, message: SessionMessageSpec):
        return await service.add_session_message(session_id, message)
    @api.post('/api/workspaces/pick')
    async def pick_workspace(request: Request):
        host = request.client.host if request.client else ''
        if host not in {'127.0.0.1', '::1', 'localhost'}:
            raise HTTPException(status_code=403, detail='Folder picker is available only from this computer')
        if platform.system() != 'Darwin':
            raise HTTPException(status_code=501, detail='Native Finder folder selection is available on macOS')
        def choose_folder():
            result = subprocess.run(
                ['/usr/bin/osascript', '-e', 'POSIX path of (choose folder with prompt "选择 EvoCode 工作区")'],
                capture_output=True, text=True, timeout=180,
            )
            if result.returncode != 0:
                if '-128' in result.stderr:
                    return None
                raise RuntimeError(result.stderr.strip() or 'Finder folder selection failed')
            from pathlib import Path
            path = str(Path(result.stdout.strip()).resolve())
            return {'path': path, 'name': Path(path).name or path}
        try:
            return await asyncio.to_thread(choose_folder)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=408, detail='Folder selection timed out')
    @api.post('/api/tasks')
    async def create(spec: TaskSpec):
        task = service.create_task(spec)
        service.schedule(task['task_id'], service.run(task['task_id']))
        return task
    @api.get('/api/tasks/{task_id}')
    def task(task_id: str): return service.store.get_task(task_id)
    @api.post('/api/tasks/{task_id}/cancel')
    def cancel(task_id: str):
        if service.store.get_task(task_id)['status'] in ('completed','failed','cancelled'):
            raise HTTPException(status_code=409,detail='任务已经结束')
        service.controller.cancel(task_id)
        return service.store.get_task(task_id)
    @api.post('/api/tasks/{task_id}/stop')
    async def stop(task_id: str):
        task=service.store.get_task(task_id)
        if task['status'] in ('completed','failed','cancelled'):
            raise HTTPException(status_code=409,detail='任务已经结束')
        return await service.stop_task(task_id)
    @api.post('/api/tasks/{task_id}/resume')
    async def resume(task_id: str):
        service.store.get_task(task_id)
        service.schedule(task_id, service.run(task_id, resume=True))
        return {'scheduled': True}
    @api.post('/api/tasks/{task_id}/resolve')
    def resolve(task_id: str, body: dict): return service.controller.resolve(task_id, body['tool_call_id'], body['resolution'])
    @api.get('/api/tasks/{task_id}/events')
    def events(task_id: str, after_seq: int=0): return service.store.read_events(task_id=task_id, after_seq=after_seq)
    @api.get('/api/tasks/{task_id}/stream')
    async def stream(task_id: str, request: Request, after_seq: int=0):
        service.store.get_task(task_id)
        try:
            after_seq = max(after_seq, int(request.headers.get('last-event-id', '0')))
        except ValueError:
            raise HTTPException(status_code=400, detail='Invalid event cursor')
        async def generate():
            cursor = after_seq
            while not await request.is_disconnected():
                for event in service.store.read_events(task_id=task_id, after_seq=cursor):
                    cursor = max(cursor, event['seq'])
                    yield f'id: {cursor}\ndata: {json.dumps(event,ensure_ascii=False)}\n\n'
                yield ': keepalive\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(generate(), media_type='text/event-stream', headers={'Cache-Control':'no-cache'})
    @api.get('/api/artifacts/{artifact_id}')
    def artifact(artifact_id: str):
        row = service.store.get_artifact(artifact_id)
        return FileResponse(row['storage_path'], media_type='text/plain')
    @api.get('/api/memory')
    def memory(): return service.memory.status()
    @api.post('/api/memory/search')
    async def search(body: dict):
        task = service.store.get_task(body['task_id'])
        return await service.memory.search(body['query'], task)
    @api.post('/api/memory/rebuild')
    async def rebuild(): return await service.memory.rebuild_index()
    @api.post('/api/memory/index-pending')
    async def index_pending(body: dict):
        ids = body.get('allowed_event_ids')
        if not isinstance(ids, list) or any(not isinstance(i, str) or not i for i in ids):
            raise ValueError('allowed_event_ids must be a list of event IDs')
        limit = body.get('limit', 100)
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError('limit must be between 1 and 5000')
        return await service.memory.index_pending(limit, allowed_event_ids=list(dict.fromkeys(ids)))
    @api.get('/api/skills')
    def skills(): return service.skills.list_skills()
    @api.get('/api/skills/versions')
    def versions(skill_id: str|None=None): return service.skills.list_versions(skill_id)
    @api.get('/api/skills/versions/{version_id}')
    def version(version_id: str): return service.skills.load(version_id)
    @api.post('/api/skills/{skill_id}/rollback')
    def rollback(skill_id: str, body: dict): return service.skills.rollback(skill_id, body['version_id'])
    @api.post('/api/skills/{skill_id}/deprecate')
    def deprecate(skill_id: str): return service.skills.deprecate(skill_id)
    @api.get('/api/evolution')
    def evolution(): return service.evolution.list_runs()
    @api.get('/api/evolution/active-meta')
    def active_meta(): return service.evolution.active_meta()
    @api.get('/api/evolution/{run_id}')
    def evolution_run(run_id: str): return service.evolution.get_run(run_id)
    @api.post('/api/evolution')
    async def trigger(body: dict):
        return await service.evolution.trigger(body.get('task_id'), kind=body.get('kind','task'), manual=True,
            trace_task_ids=body.get('trace_task_ids'), idempotency_key=body.get('idempotency_key'),
            meta_history_run_ids=body.get('meta_history_run_ids'))
    @api.post('/api/evolution/external-candidates/evaluate', status_code=202)
    async def queue_external_candidate_evaluation(body: dict):
        batch_id = body.get('batch_id')
        runs = service.evolution.queue_external_candidate_batch(
            batch_id, body.get('frozen_memory_collection'), body.get('audit_fixture_root'), body.get('candidates'))
        job_key = f'external-audit:{batch_id}'
        job = service.jobs.get(job_key)
        if job is None or job.done():
            service.evolution.interrupt_abandoned_external_candidate_runs(batch_id)
            queued_ids = [run['run_id'] for run in service.evolution.list_runs()
                          if run['kind'] == 'external_task'
                          and (run['payload'].get('external_candidate_audit') or {}).get('batch_id') == batch_id
                          and run['status'] == 'queued']
            if queued_ids:
                service.schedule(job_key, service.evolution.evaluate_external_candidate_runs(queued_ids))
        batch = service.evolution.get_external_candidate_batch(batch_id)
        return {
            'batch_id': batch_id,
            'status': batch['status'],
            'runs': [{key: record[key] for key in ('candidate_id', 'run_id', 'status')}
                     for record in batch['records']],
            'status_url': f'/api/evolution/external-candidates/{batch_id}',
        }
    @api.get('/api/evolution/external-candidates/{batch_id}')
    def external_candidate_batch(batch_id: str):
        job = service.jobs.get(f'external-audit:{batch_id}')
        if job is None or job.done():
            service.evolution.interrupt_abandoned_external_candidate_runs(batch_id)
        return service.evolution.get_external_candidate_batch(batch_id)
    @api.post('/api/evolution/{run_id}/evaluate')
    async def evaluate(run_id: str, body: dict):
        collection = body.get('frozen_memory_collection')
        if collection is not None and (not isinstance(collection, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', collection)):
            raise ValueError('Invalid frozen_memory_collection')
        service.schedule('eval:'+run_id, service.evolution.evaluate(run_id, body['task_specs'],
            frozen_memory_collection=collection))
        return {'scheduled': True}
    @api.post('/api/evolution/{run_id}/publish')
    def publish(run_id: str): return service.evolution.publish(run_id)
    @api.post('/api/evolution/{run_id}/rollback')
    def rollback_merge(run_id: str): return service.evolution.rollback_merge(run_id)
    @api.get('/api/config')
    def config_get(): return service.config.snapshot()
    @api.put('/api/config')
    def config_put(body: dict):
        if isinstance(body.get('evolution'), dict) and 'selection_task_specs' in body['evolution']:
            raise ValueError('自动演化验收任务只能通过服务端配置设置')
        updated = AppConfig.model_validate(body)
        if updated.data_dir != service.config.data_dir:
            raise ValueError('运行中不能修改data_dir')
        import yaml
        updated.evolution.selection_task_specs = service.config.evolution.selection_task_specs
        path = Path(os.getenv('EVOCODE_CONFIG', 'config/local.yaml'))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(updated.model_dump(), allow_unicode=True))
        # Preserve references held by modules, while tasks retain serialized snapshots.
        for field in AppConfig.model_fields:
            setattr(service.config, field, getattr(updated, field))
        return {'config': updated.snapshot(), 'effective': 'next_task'}
    dist = Path(__file__).resolve().parents[2] / 'web/dist'
    if dist.exists():
        api.mount('/', StaticFiles(directory=dist, html=True), name='web')
    return api
