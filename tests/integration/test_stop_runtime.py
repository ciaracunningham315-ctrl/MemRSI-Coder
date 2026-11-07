import asyncio
import json
import os
import sys

from evocode.api import create_app
from evocode.app import Application
from evocode.config import AppConfig
from evocode.contracts import (
    ExecutionAssignment,
    LLMResponse,
    PlannerDecision,
    RequirementsProposal,
    SessionMessageSpec,
    TaskPlan,
    TaskSpec,
    ToolCall,
)


def configuration(tmp_path):
    cfg=AppConfig(data_dir=str(tmp_path/'data'))
    cfg.raw_rag.enabled=False
    cfg.raw_rag.background_indexing=False
    cfg.skills.enabled=False
    cfg.evolution.enabled=False
    cfg.runtime.execution_backend='host'
    return cfg


class DelayedModel:
    def __init__(self):
        self.entered=asyncio.Event()
        self.calls=0

    async def complete(self, messages, *, role='coder', **kwargs):
        self.calls+=1
        if self.calls==1:
            self.entered.set()
            await asyncio.Event().wait()
        context = json.loads(messages[-1]['content'].split('\n', 1)[1])
        return LLMResponse(content=json.dumps({
            'action': 'respond', 'answer': '已按补充要求处理。',
            'requirements': {'source_user_seq': context['consumed_user_seq'], 'goal': 'Follow the correction'},
        }))


async def test_stop_during_model_wait_then_correction_resumes_same_task_once(tmp_path):
    model=DelayedModel()
    app=Application(configuration(tmp_path),model)
    session=app.create_session(str(tmp_path))['session']['session_id']
    try:
        first=await app.add_session_message(session,SessionMessageSpec(content='先做初始任务'))
        task_id=first['task_id']
        await asyncio.wait_for(model.entered.wait(),2)

        stopped=await app.stop_task(task_id)
        assert stopped['status']=='interrupted'

        correction=await app.add_session_message(session,SessionMessageSpec(content='改为按补充要求处理'))
        assert correction['task_id']==task_id
        finished=await asyncio.wait_for(app.jobs[task_id],3)
        assert finished['status']=='completed'
        messages=app.store.get_session(session)['messages']
        finals=[message for message in messages if message['kind']=='final']
        assert len(finals)==1
        assert finals[0]['content']=='已按补充要求处理。'
        assert model.calls==2
    finally:
        await app.close()


async def test_stop_before_scheduled_runner_starts_persists_interrupted(tmp_path):
    app=Application(configuration(tmp_path),DelayedModel())
    try:
        task=app.create_task(TaskSpec(repo_path=str(tmp_path),prompt='stop before start'))
        app.schedule(task['task_id'],app.run(task['task_id']))

        stopped=await app.stop_task(task['task_id'])
        assert stopped['status']=='interrupted'
        assert app.jobs[task['task_id']].cancelled()
        assert app.store.latest_checkpoint(task['task_id']) is None
    finally:
        await app.close()


async def test_stop_during_tool_keeps_uncertain_effect_for_review(tmp_path):
    app=Application(configuration(tmp_path),DelayedModel())
    task=app.create_task(TaskSpec(repo_path=str(tmp_path),prompt='exercise an in-flight tool'))
    entered=asyncio.Event()

    async def plan(*args,**kwargs):
        return PlannerDecision(
            action='delegate',
            requirements=RequirementsProposal(source_user_seq=1, goal='inspect'),
            plan=TaskPlan(goal='inspect',steps=[{'id':'step1','goal':'inspect'}]),
            assignment=ExecutionAssignment(step_id='step1',goal='inspect'),
        )

    async def coder(*args,**kwargs):
        return LLMResponse(tool_calls=[ToolCall(id='tool-1',name='run_command',arguments={'command':['sh','-c','effect']})])

    async def pending_execute(task, state, executor, call):
        app.store.append_event(task['task_id'],'tool.started',{
            'tool_call_id':call.id,'tool':call.name,'arguments':call.arguments,'prepared':{},
        })
        entered.set()
        await asyncio.Event().wait()

    app.controller._plan=plan
    app.controller.coder.complete=coder
    app.controller._execute=pending_execute
    try:
        app.schedule(task['task_id'],app.run(task['task_id']))
        await asyncio.wait_for(entered.wait(),2)
        stopped=await app.stop_task(task['task_id'])
        assert stopped['status']=='needs_review'
        assert stopped['state']['uncertain_tools'][0]['tool_call_id']=='tool-1'
        assert app.controller.recovery.uncertain(task['task_id'])
        assert stopped['state'].get('final_message_id') is None
    finally:
        await app.close()


async def test_stop_kills_real_command_before_marker_and_is_safe_when_repeated(tmp_path):
    app=Application(configuration(tmp_path),DelayedModel())
    task=app.create_task(TaskSpec(repo_path=str(tmp_path),prompt='run and stop a subprocess'))
    marker=tmp_path/'command-finished'

    async def plan(*args,**kwargs):
        return PlannerDecision(
            action='delegate',
            requirements=RequirementsProposal(source_user_seq=1, goal='inspect'),
            plan=TaskPlan(goal='run a command',steps=[{'id':'step1','goal':'run a command'}]),
            assignment=ExecutionAssignment(step_id='step1',goal='run a command'),
        )

    async def coder(*args,**kwargs):
        code=(f"import time; from pathlib import Path; time.sleep(30); "
              f"Path({str(marker)!r}).write_text('completed')")
        return LLMResponse(tool_calls=[ToolCall(
            id='real-process',name='run_command',arguments={'command':[sys.executable,'-c',code]},
        )])

    app.controller._plan=plan
    app.controller.coder.complete=coder
    try:
        app.schedule(task['task_id'],app.run(task['task_id']))
        process=None
        for _ in range(300):
            executor=app.controller._executors.get(task['task_id'])
            if executor and executor.processes:
                process=executor.processes[task['task_id']]
                break
            await asyncio.sleep(.01)
        assert process is not None, 'run_command subprocess was not registered'
        assert process.returncode is None
        assert any(event['event_type']=='tool.started' for event in app.store.read_events(task_id=task['task_id']))

        stopped=await asyncio.gather(app.stop_task(task['task_id']),app.stop_task(task['task_id']))
        assert all(item['status']=='needs_review' for item in stopped)
        assert process.returncode is not None
        try:
            os.killpg(process.pid,0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError('command process group still exists after stop')
        assert not marker.exists()
        events=app.store.read_events(task_id=task['task_id'])
        assert any(event['event_type']=='tool.interrupted' and event['payload']['tool_call_id']=='real-process' for event in events)
        assert 'real-process' in app.controller.recovery.uncertain(task['task_id'])
    finally:
        await app.close()


def test_stop_endpoint_returns_persisted_task_state(tmp_path):
    app=Application(configuration(tmp_path),DelayedModel())
    task=app.create_task(TaskSpec(repo_path=str(tmp_path),prompt='stop via API'))
    from fastapi.testclient import TestClient
    with TestClient(create_app(application=app)) as client:
        response=client.post(f"/api/tasks/{task['task_id']}/stop")
        assert response.status_code==200
        assert response.json()['status']=='interrupted'
        assert client.get(f"/api/tasks/{task['task_id']}").json()['status']=='interrupted'
