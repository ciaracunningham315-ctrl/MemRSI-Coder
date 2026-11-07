import asyncio
import json
import shutil
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from evocode.config import AppConfig
from evocode.contracts import ExecutionAssignment, EvidenceRef, RetrievalResult, TaskPlan, TaskSpec, LLMResponse, ToolCall
from evocode.persistence import EventStore
from evocode.agent import AgentController
from evocode.agent.tools import ToolExecutor


def planner_response(messages, decision):
    """Encode a scripted decision using the current requirements protocol."""
    context = json.loads(messages[-1]['content'].split('\n', 1)[1])
    current = context['current_requirements']
    sequence = context['consumed_user_seq']
    if not current or current['source_user_seq'] != sequence:
        goal = decision.get('assignment', {}).get('goal') or context['latest_user_requirement']
        decision['requirements'] = {
            'expected_revision': current['revision'] if current else 0,
            'source_user_seq': sequence,
            'goal': goal,
        }
    return LLMResponse(content=json.dumps(decision))


class Model:
    def __init__(self, outputs, planner_plan=None):
        self.outputs=iter(outputs);self.roles=[];self.requests=[];self.planner_calls=0
        self.planner_plan=planner_plan
    async def complete(self,messages,*,role,**kwargs):
        self.roles.append(role)
        self.requests.append({'role':role,'messages':messages,'kwargs':kwargs})
        if role=='planner':
            self.planner_calls+=1
            joined = json.dumps(messages, ensure_ascii=False)
            if self.planner_calls==1 or 'Acceptance verification failed' in joined:
                decision={'action':'delegate','assignment':{'step_id':'s1','goal':'edit the requested files'}}
                if self.planner_plan is not None:
                    decision['plan']=self.planner_plan
            else:
                decision={'action':'respond','answer':'Completed the requested work.'}
            return planner_response(messages, decision)
        try:return next(self.outputs)
        except StopIteration:return LLMResponse(content=json.dumps({'step_id':'s1','outcome':'completed','summary':'Finished the assigned work.'}))


def report(summary='Finished the assigned work.'):
    return LLMResponse(content=json.dumps({'step_id':'s1','outcome':'completed','summary':summary}))


@pytest.fixture
def setup(tmp_path):
    repo=tmp_path/'repo';repo.mkdir()
    config=AppConfig(data_dir=str(tmp_path/'data'))
    config.runtime.execution_backend='host';config.raw_rag.enabled=False;config.skills.enabled=False
    store=EventStore(config.sqlite_path)
    yield repo,config,store
    store.close()


def task(setup,command=None):
    repo,config,store=setup
    return store.create_task(TaskSpec(repo_path=str(repo),prompt='fix',acceptance_command=command or []),config.snapshot())


def test_atomic_sequence_and_rollback(setup):
    _,_,store=setup;t=task(setup)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i:store.append_event(t['task_id'],'test',{'i':i}),range(20)))
    events=store.read_events(task_id=t['task_id'])
    assert [e['seq'] for e in events]==list(range(1,22))
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.update_task(t['task_id'],status='running');raise RuntimeError()
    assert store.get_task(t['task_id'])['status']=='created'
    assert len(store.read_events(task_id=t['task_id']))==21


def test_real_edit_and_acceptance(setup):
    repo,config,store=setup
    (repo/'value.py').write_text('answer = 1\n')
    t=task(setup,[sys.executable,'-c','from value import answer; assert answer == 2'])
    model=Model([LLMResponse(tool_calls=[ToolCall(id='c1',name='edit_file',arguments={'path':'value.py','old_text':'answer = 1','new_text':'answer = 2'})]),report('Updated the answer.')])
    result=asyncio.run(AgentController(store,model,config).run(t['task_id']))
    assert result['status']=='completed',result
    assert result['state']['verification']['status']=='passed'
    assert model.roles==['planner','coder','coder','planner']
    assert store.latest_checkpoint(t['task_id'])


def test_false_success_fails_acceptance(setup):
    _,config,store=setup;t=task(setup,[sys.executable,'-c','raise SystemExit(1)'])
    result=asyncio.run(AgentController(store,Model([report('The implementation appears complete.')]),config).run(t['task_id']))
    assert result['status']=='failed'
    assert result['state']['verification']['status']=='failed'
    assert result['state']['verification']['status']=='failed'


def test_unknown_side_effect_does_not_repeat(setup):
    _,config,store=setup;t=task(setup)
    store.append_event(t['task_id'],'tool.started',{'tool_call_id':'lost','tool':'run_command','arguments':{'command':['echo','side effect']}})
    runner=AgentController(store,Model([]),config)
    result=asyncio.run(runner.resume(t['task_id']))
    assert result['status']=='needs_review'
    runner.resolve(t['task_id'],'lost','completed')
    assert not runner._uncertain(t['task_id'])


def test_edit_recovery_and_path_escape(setup):
    repo,config,store=setup;t=task(setup)
    executor=ToolExecutor(store,config)
    call=ToolCall(id='edit',name='edit_file',arguments={'path':'a.py','old_text':'','new_text':'x = 1'})
    prepared=executor.prepare(t,call)
    store.append_event(t['task_id'],'tool.started',{'tool_call_id':'edit','tool':'edit_file','prepared':prepared})
    asyncio.run(executor.execute(t,call,prepared))
    result=asyncio.run(AgentController(store,Model([report()]),config).resume(t['task_id']))
    assert result['status']=='completed'
    assert any(e['event_type']=='tool.resolved' for e in store.read_events(task_id=t['task_id']))
    with pytest.raises(ValueError):executor.path(repo,'../secret')
    (repo/'link').symlink_to(repo.parent)
    with pytest.raises(ValueError):executor.path(repo,'link/secret')


def test_memory_gate_and_artifact_integrity(setup):
    _,config,store=setup;t=task(setup)
    class Memory:
        async def search(self,*args):raise AssertionError('Must not run')
    result=asyncio.run(ToolExecutor(store,config,Memory()).execute(t,ToolCall(id='m',name='memory_search',arguments={'query':'past'})))
    assert result.status=='disabled'
    artifact=store.save_artifact('original')
    Path(artifact['path']).write_text('changed')
    with pytest.raises(ValueError):store.get_artifact(artifact['artifact_id'])


def test_isolated_query_precedes_deduplicates_and_preserves_planner_searches(setup):
    _,config,store=setup
    config.raw_rag.enabled=True
    config.raw_rag.background_indexing=False
    current=task(setup)
    prompt=current['prompt']
    event_id='source-event-1'
    class Memory:
        def __init__(self):
            self.queries=[]
        async def search(self,query,snapshot):
            self.queries.append((query,snapshot['repository_id']))
            return RetrievalResult(status='ok',mode='hybrid',evidence=[EvidenceRef(
                event_id=event_id,session_id=current['session_id'],task_id='source-task',
                text='Observed project cache-key namespace: identity:session:<session_id>.'
            )])
    memory=Memory()
    model=Model([report()],planner_plan={
        'goal':'edit the requested files','steps':[{'id':'s1','goal':'edit the requested files'}],
        'memory_queries':[prompt,prompt+' distinct planner query'],
    })
    result=asyncio.run(AgentController(
        store,model,config,memory,automatic_memory_query=prompt,
    ).run(current['task_id']))
    assert result['status']=='completed',result
    assert memory.queries==[
        (prompt,current['repository_id']),
        (prompt+' distinct planner query',current['repository_id']),
    ]
    events=store.read_events(task_id=current['task_id'])
    retrieval=next(e for e in events if e['event_type']=='memory.retrieved')
    assert retrieval['payload']['query']==prompt
    context_event=next(e for e in events if e['event_type']=='context.assembled' and e['payload']['role']=='coder')
    assert event_id in context_event['payload']['node_context']['evidence_refs']
    coder_request=next(request for request in model.requests if request['role']=='coder')
    assert 'identity:session:<session_id>' in json.dumps(coder_request['messages'])

    config.raw_rag.enabled=False
    off_task=task(setup)
    off_model=Model([report()])
    off_memory=Memory()
    off_result=asyncio.run(AgentController(
        store,off_model,config,off_memory,automatic_memory_query=off_task['prompt'],
    ).run(off_task['task_id']))
    assert off_result['status']=='completed',off_result
    assert off_memory.queries==[]
    off_events=store.read_events(task_id=off_task['task_id'])
    assert not any(event['event_type']=='memory.retrieved' for event in off_events)
    off_coder=next(request for request in off_model.requests if request['role']=='coder')
    assert 'identity:session:<session_id>' not in json.dumps(off_coder['messages'])
    assert all(tool['function']['name']!='memory_search'
               for tool in off_coder['kwargs'].get('tools') or [])


def test_cancel_kills_process(setup):
    _,config,store=setup;t=task(setup)
    model=Model([LLMResponse(tool_calls=[ToolCall(id='sleep',name='run_command',arguments={'command':[sys.executable,'-c','import time;time.sleep(30)']})])])
    runner=AgentController(store,model,config)
    async def scenario():
        running=asyncio.create_task(runner.run(t['task_id']))
        for _ in range(100):
            await asyncio.sleep(.01)
            executor=runner._executors.get(t['task_id'])
            if executor and executor.processes:break
        runner.cancel(t['task_id'])
        return await asyncio.wait_for(running,3)
    result=asyncio.run(scenario())
    assert result['status']=='cancelled'
    assert runner._uncertain(t['task_id']) # cancellation is not proof a command had no side effects


def test_timed_out_command_with_partial_effect_requires_review(setup):
    repo,config,store=setup
    config.runtime.tool_timeout=1
    t=task(setup)
    model=Model([LLMResponse(tool_calls=[ToolCall(id='partial',name='run_command',arguments={
        'command':[sys.executable,'-c',"from pathlib import Path; Path('partial.txt').write_text('started'); import time; time.sleep(30)"]
    })])])
    runner=AgentController(store,model,config)
    result=asyncio.run(runner.run(t['task_id']))
    assert result['status']=='needs_review'
    assert (repo/'partial.txt').read_text()=='started'
    assert 'partial' in runner._uncertain(t['task_id'])
    resumed=asyncio.run(runner.resume(t['task_id']))
    assert resumed['status']=='needs_review'
    assert (repo/'partial.txt').read_text()=='started'


def test_tool_events_include_real_git_and_patch_evidence(setup):
    repo,config,store=setup
    if not shutil.which('git'):
        pytest.skip('git is not installed')
    (repo/'app.py').write_text('value = 1\n')
    subprocess.run(['git','init','-q',str(repo)],check=True)
    subprocess.run(['git','-C',str(repo),'config','user.email','runtime@example.test'],check=True)
    subprocess.run(['git','-C',str(repo),'config','user.name','Runtime Test'],check=True)
    subprocess.run(['git','-C',str(repo),'add','app.py'],check=True)
    subprocess.run(['git','-C',str(repo),'commit','-qm','initial'],check=True)
    commit=subprocess.run(['git','-C',str(repo),'rev-parse','HEAD'],check=True,capture_output=True,text=True).stdout.strip()
    t=task(setup)
    calls=[
        ToolCall(id='read',name='read_file',arguments={'path':'app.py'}),
        ToolCall(id='edit',name='edit_file',arguments={'path':'app.py','old_text':'value = 1','new_text':'value = 2'}),
        ToolCall(id='cmd',name='run_command',arguments={'command':[sys.executable,'-c',"from pathlib import Path; Path('generated.txt').write_text('ok')"]}),
    ]
    model=Model([LLMResponse(tool_calls=calls),report()])
    result=asyncio.run(AgentController(store,model,config).run(t['task_id']))
    assert result['status']=='completed'
    events=store.read_events(task_id=t['task_id'])
    completed={e['payload']['tool_call_id']:e['payload'] for e in events if e['event_type']=='tool.completed'}
    assert completed['read']['git_commit']==commit
    assert completed['read']['file_paths']==['app.py']
    assert completed['edit']['git_commit']==commit
    assert completed['edit']['file_paths']==['app.py']
    assert completed['cmd']['git_commit']==commit
    assert completed['cmd']['file_paths']==['generated.txt']
    patch_event=next(e['payload'] for e in events if e['event_type']=='code.patch')
    assert patch_event['file_paths']==['app.py']
    assert patch_event['git_commit']==commit
    assert '--- a/app.py\n+++ b/app.py\n' in patch_event['patch']
    assert store.get_artifact(patch_event['artifact_id'])['kind']=='code_patch'
    assert Path(store.get_artifact(patch_event['artifact_id'])['path']).read_text()==patch_event['patch']


def test_task_uses_frozen_config_bound_llm_without_mutating_controller(setup):
    repo,config,store=setup
    snapshot=config.snapshot()
    snapshot['models']['local']['model_id']='task-snapshot-model'
    t=store.create_task(TaskSpec(repo_path=str(repo),prompt='fix'),snapshot)
    config.models.local.model_id='changed-global-model'

    class ConfigurableModel:
        def __init__(self):
            self.bound_model_ids=[]
            self.roles=[]
        def for_config(self, task_config):
            self.bound_model_ids.append(task_config.models.local.model_id)
            owner=self
            class Bound:
                async def complete(self,messages,*,role,**kwargs):
                    owner.roles.append(role)
                    if role=='planner':
                        decision = {'action':'delegate','assignment':{'step_id':'s1','goal':'edit'}} if owner.roles.count('planner') == 1 else {'action':'respond','answer':'Finished.'}
                        return planner_response(messages, decision)
                    return report('finished')
            return Bound()

    llm=ConfigurableModel()
    runner=AgentController(store,llm,config)
    result=asyncio.run(runner.run(t['task_id']))
    assert result['status']=='completed'
    assert llm.bound_model_ids==['task-snapshot-model']
    assert llm.roles==['planner','coder','planner']
    assert runner.llm is llm


def test_planner_can_answer_without_starting_coder(setup):
    repo, config, store = setup
    t = task(setup)

    class PlannerOnly:
        async def complete(self, messages, *, role, **kwargs):
            assert role == 'planner'
            return planner_response(messages, {'action': 'respond', 'answer': 'Hello.'})

    result = asyncio.run(AgentController(store, PlannerOnly(), config).run(t['task_id']))
    assert result['status'] == 'completed'
    assert result['state']['next_node'] == 'end'
    assert result['state'].get('user_addenda', []) == []
    assert [e['event_type'] for e in store.read_events(task_id=t['task_id'])].count('assistant.final') == 1
    assert not any(e['event_type'].startswith('coder.') for e in store.read_events(task_id=t['task_id']))


def test_coder_report_returns_control_to_planner_before_final(setup):
    repo, config, store = setup
    t = task(setup)

    class Script:
        def __init__(self): self.roles = []
        async def complete(self, messages, *, role, **kwargs):
            self.roles.append(role)
            if role == 'planner' and self.roles.count('planner') == 1:
                return planner_response(messages, {'action': 'delegate', 'assignment': {'step_id': 's1', 'goal': 'inspect'}})
            if role == 'coder':
                return LLMResponse(content=json.dumps({'step_id': 's1', 'outcome': 'completed', 'summary': 'Inspected the repository.'}))
            return LLMResponse(content=json.dumps({'action': 'respond', 'answer': 'The repository is ready.'}))

    model = Script()
    result = asyncio.run(AgentController(store, model, config).run(t['task_id']))
    events = store.read_events(task_id=t['task_id'])
    assert result['status'] == 'completed'
    assert model.roles == ['planner', 'coder', 'planner']
    assert next(i for i, event in enumerate(events) if event['event_type'] == 'coder.report') < next(i for i, event in enumerate(events) if event['event_type'] == 'assistant.final')
    assert len([event for event in events if event['event_type'] == 'assistant.final']) == 1


def test_planner_question_resumes_same_task_on_user_reply(setup):
    repo, config, store = setup
    t = task(setup)

    class Script:
        def __init__(self): self.planner_calls = 0
        async def complete(self, messages, *, role, **kwargs):
            assert role == 'planner'
            self.planner_calls += 1
            if self.planner_calls == 1:
                return planner_response(messages, {'action': 'ask_user', 'question': 'Which file should I inspect?'})
            return planner_response(messages, {'action': 'respond', 'answer': 'I will inspect app.py.'})

    model = Script()
    runner = AgentController(store, model, config)
    waiting = asyncio.run(runner.run(t['task_id']))
    assert waiting['status'] == 'waiting_user'
    store.append_session_event(t['session_id'], 'user.message', {
        'content': 'Inspect app.py', 'intent': 'auto', 'target_task_id': t['task_id'],
    }, task_id=t['task_id'])
    completed = asyncio.run(runner.resume(t['task_id']))
    assert completed['task_id'] == t['task_id']
    assert completed['status'] == 'completed'
    assert model.planner_calls == 2


def test_workspace_clarification_cannot_publish_unwritten_files(setup):
    repo, config, store = setup
    prompt = f"为我生成一段fastapi最小实现代码在'{repo.parent / 'text2'}'"
    t = store.create_task(TaskSpec(repo_path=str(repo), prompt=prompt), config.snapshot())

    class Script:
        calls = 0

        async def complete(self, messages, *, role, **kwargs):
            if role == 'coder':
                if (repo / 'text2/main.py').exists():
                    return report()
                return LLMResponse(tool_calls=[ToolCall(id='write-main', name='edit_file', arguments={
                    'path': 'text2/main.py', 'old_text': '', 'new_text': 'app = None\n',
                })])
            self.calls += 1
            if self.calls == 1:
                return planner_response(messages, {'action': 'ask_user', 'question': '写到当前工作区的子目录可以吗？'})
            if self.calls == 2:
                decision = planner_response(messages, {'action': 'respond', 'answer': '已生成文件。'})
                payload = json.loads(decision.content)
                payload['requirements']['delivery_required'] = True
                return LLMResponse(content=json.dumps(payload))
            if self.calls == 3:
                assert 'needs concrete create or modify delivery targets' in json.dumps(messages)
                decision = planner_response(messages, {'action': 'delegate', 'assignment': {
                    'step_id': 's1', 'goal': 'Write the requested file',
                }})
                payload = json.loads(decision.content)
                payload['requirements']['delivery_required'] = True
                payload['requirements']['delivery_targets'] = [{'path': 'text2/main.py', 'operation': 'create'}]
                return LLMResponse(content=json.dumps(payload))
            return LLMResponse(content='{"action":"respond","answer":"文件已生成。"}')

    runner = AgentController(store, Script(), config)
    assert asyncio.run(runner.run(t['task_id']))['status'] == 'waiting_user'
    store.append_session_event(t['session_id'], 'user.message', {
        'content': '在当前工作区新建一个文件夹，把fastapi最小实现放进去',
        'intent': 'auto', 'target_task_id': t['task_id'],
    }, task_id=t['task_id'])
    result = asyncio.run(runner.resume(t['task_id']))
    assert result['status'] == 'completed', result['state'].get('error')
    assert (repo / 'text2/main.py').read_text() == 'app = None\n'
    events = store.read_events(task_id=t['task_id'])
    assert any(event['event_type'] == 'delivery.verification' and event['payload']['status'] == 'passed' for event in events)
    finals = [event for event in events if event['event_type'] == 'assistant.final']
    assert [event['payload']['content'] for event in finals] == ['文件已生成。']
    assert next(event['seq'] for event in events if event['event_type'] == 'code.patch') < finals[0]['seq']


def test_new_input_during_coder_generation_skips_stale_tool(setup):
    repo, config, store = setup
    t = task(setup)

    class SteeringModel:
        def __init__(self): self.planner_calls = 0
        async def complete(self, messages, *, role, **kwargs):
            if role == 'planner':
                self.planner_calls += 1
                action = {'action': 'delegate', 'assignment': {'step_id': 's1', 'goal': 'make a change'}} if self.planner_calls == 1 else {'action': 'respond', 'answer': 'I will answer the updated request.'}
                return planner_response(messages, action)
            store.append_session_event(t['session_id'], 'user.message', {
                'content': 'Do not edit files; just explain the issue.', 'intent': 'redirect', 'target_task_id': t['task_id'],
            }, task_id=t['task_id'])
            return LLMResponse(tool_calls=[ToolCall(id='stale-edit', name='edit_file', arguments={
                'path': 'should-not-exist.txt', 'old_text': '', 'new_text': 'stale',
            })])

    result = asyncio.run(AgentController(store, SteeringModel(), config).run(t['task_id']))
    assert result['status'] == 'completed'
    assert not (repo / 'should-not-exist.txt').exists()
    events = store.read_events(task_id=t['task_id'])
    updates = [event for event in events if event['event_type'] == 'task.updated']
    assert any('skipped' in json.dumps(event['payload'].get('state', {}).get('coder_messages', [])) for event in updates)
    assert len([event for event in events if event['event_type'] == 'assistant.final']) == 1


def test_multi_step_plan_preserves_completed_step_and_routes_each_report(setup):
    repo, config, store = setup
    t = task(setup)

    class MultiStep:
        def __init__(self): self.planner_calls = 0; self.coder_calls = 0
        async def complete(self, messages, *, role, **kwargs):
            if role == 'planner':
                self.planner_calls += 1
                if self.planner_calls == 1:
                    plan = {'goal': 'Inspect then summarize', 'steps': [
                        {'id': 's1', 'goal': 'Inspect files', 'status': 'pending'},
                        {'id': 's2', 'goal': 'Summarize', 'status': 'pending'},
                    ]}
                    decision = {'action': 'delegate', 'plan': plan, 'assignment': {'step_id': 's1', 'goal': 'Inspect files'}}
                elif self.planner_calls == 2:
                    decision = {'action': 'delegate', 'assignment': {'step_id': 's2', 'goal': 'Summarize'}}
                else:
                    decision = {'action': 'respond', 'answer': 'Both steps are complete.'}
                return planner_response(messages, decision)
            self.coder_calls += 1
            return LLMResponse(content=json.dumps({
                'step_id': f's{self.coder_calls}', 'outcome': 'completed', 'summary': f'Completed s{self.coder_calls}',
            }))

    result = asyncio.run(AgentController(store, MultiStep(), config).run(t['task_id']))
    assert result['status'] == 'completed'
    assert {key: value['status'] for key, value in result['state']['steps'].items()} == {'s1': 'completed', 's2': 'completed'}
    events = store.read_events(task_id=t['task_id'])
    transitions = [event['payload'] for event in events if event['event_type'] == 'node.transition']
    assert [('planner', 'coder'), ('coder', 'planner'), ('planner', 'coder'), ('coder', 'planner'), ('planner', 'end')] == [
        (entry['from'], entry['to']) for entry in transitions
    ]


def test_planner_paraphrasing_completed_goal_does_not_repeat_coder(setup):
    _,cfg,store=setup;t=task(setup)
    class Repeating:
        def __init__(self):self.planners=0;self.coders=0
        async def complete(self,messages,*,role,**kwargs):
            if role=='coder':
                self.coders+=1
                return LLMResponse(content='{"step_id":"s1","outcome":"completed","summary":"Done and checked"}')
            self.planners+=1
            if self.planners==3:
                assert 'already completed' in json.dumps(messages)
                return LLMResponse(content='{"action":"respond","answer":"Finished once."}')
            goal='fix the file' if self.planners==1 else 'repair the same file'
            return planner_response(messages, {'action':'delegate','plan':{'goal':'fix','steps':[{'id':'s1','goal':goal}]},'assignment':{'step_id':'s1','goal':goal}})
    model=Repeating()
    result=asyncio.run(AgentController(store,model,cfg).run(t['task_id']))
    assert result['status']=='completed',result
    assert model.coders==1
    assert result['state']['steps']['s1']['goal']=='fix the file'


def test_git_snapshot_does_not_include_sibling_workspace_files(setup):
    repo,cfg,store=setup
    subprocess.run(['git','init','-q',str(repo)],check=True)
    (repo/'outside.txt').write_text('outside')
    nested=repo/'workspace';nested.mkdir();(nested/'inside.txt').write_text('inside')
    snapshot=ToolExecutor(store,cfg)._git_snapshot(nested)
    assert snapshot['file_paths']==['inside.txt']


def seed_older_transcript(store, task):
    for index in range(12):
        store.append_session_event(task['session_id'], 'assistant.progress', {'content': f'progress {index}'}, task_id=task['task_id'])


def test_planner_reads_missing_history_once_and_skips_exhausted_range(setup):
    repo, config, store = setup
    t = task(setup)
    seed_older_transcript(store, t)

    class HistoryToolModel:
        def __init__(self): self.planner_calls = 0; self.tool_schema = None
        async def complete(self, messages, *, role, **kwargs):
            if role != 'planner': raise AssertionError('Planner-only response expected')
            if messages[0]['content'].startswith('Create a concise durable summary'):
                return LLMResponse(content='Earlier progress.')
            self.planner_calls += 1
            self.tool_schema = kwargs.get('tools')
            if self.planner_calls == 1:
                return LLMResponse(tool_calls=[ToolCall(id='history-1', name='read_session_messages', arguments={'before_seq': 3, 'limit': 100})])
            if self.planner_calls == 2:
                last = json.loads(next(message['content'] for message in messages if message.get('tool_call_id') == 'history-1'))
                return LLMResponse(tool_calls=[
                    ToolCall(id='history-duplicate', name='read_session_messages', arguments={'after_seq': 0, 'before_seq': 3, 'limit': 100}),
                    ToolCall(id='history-tail', name='read_session_messages', arguments={'after_seq': last['next_after_seq'], 'before_seq': 3}),
                ])
            return planner_response(messages, {'action': 'respond', 'answer': 'Your request was to fix.'})

    model = HistoryToolModel()
    result = asyncio.run(AgentController(store, model, config).run(t['task_id']))
    assert result['status'] == 'completed'
    assert model.tool_schema[0]['function']['name'] == 'read_session_messages'
    events = store.read_events(task_id=t['task_id'])
    assert [e['payload']['tool_call_id'] for e in events if e['event_type'] == 'planner.tool.started'] == ['history-1']
    results = [e['payload']['result'] for e in events if e['event_type'] == 'planner.tool.completed']
    assert results[0]['messages'][0]['content'] == 'fix'
    assert [entry['status'] for entry in results] == ['completed', 'skipped', 'skipped']


def test_new_input_racing_final_publication_replans_before_one_final(setup):
    repo, config, store = setup
    t = task(setup)

    class RacingModel:
        def __init__(self): self.planner_calls = 0
        async def complete(self, messages, *, role, **kwargs):
            assert role == 'planner'
            self.planner_calls += 1
            if self.planner_calls == 1:
                store.append_session_event(t['session_id'], 'user.message', {
                    'content': 'Actually, answer in one sentence.', 'intent': 'auto', 'target_task_id': t['task_id'],
                }, task_id=t['task_id'])
                return planner_response(messages, {'action': 'respond', 'answer': 'Stale answer.'})
            return planner_response(messages, {'action': 'respond', 'answer': 'Updated one sentence.'})

    result = asyncio.run(AgentController(store, RacingModel(), config).run(t['task_id']))
    assert result['state']['final_answer'] == 'Updated one sentence.'
    finals = [event for event in store.read_events(task_id=t['task_id']) if event['event_type'] == 'assistant.final']
    assert len(finals) == 1 and finals[0]['payload']['content'] == 'Updated one sentence.'


def test_acceptance_failure_cannot_be_bypassed_by_planner_response(setup):
    repo, config, store = setup
    t = task(setup, [sys.executable, '-c', 'raise SystemExit(1)'])
    config.runtime.max_replans = 1

    class AcceptanceModel:
        def __init__(self): self.planner_calls = 0
        async def complete(self, messages, *, role, **kwargs):
            if role == 'planner':
                self.planner_calls += 1
                decision = {'action': 'delegate', 'assignment': {'step_id': 's1', 'goal': 'implement'}} if self.planner_calls == 1 else {'action': 'respond', 'answer': 'Everything passed.'}
                return planner_response(messages, decision)
            return report('Implementation done.')

    result = asyncio.run(AgentController(store, AcceptanceModel(), config).run(t['task_id']))
    assert result['status'] == 'failed'
    assert result['state']['verification']['status'] == 'failed'
    assert not any(event['event_type'] == 'assistant.final' for event in store.read_events(task_id=t['task_id']))


def test_resume_after_tool_transition_continues_coder_without_repeating_tool(setup):
    repo, config, store = setup
    t = task(setup)
    state = t['state']
    state.update({
        'next_node': 'tool',
        'assignment': {'step_id': 's1', 'goal': 'inspect', 'requirements_revision': 1},
        'current_requirements': {'revision': 1, 'source_user_seq': 1, 'goal': 'inspect'},
        'plan': {'goal': 'inspect', 'steps': [{'id': 's1', 'goal': 'inspect', 'status': 'in_progress'}]},
        'steps': {'s1': {'id': 's1', 'goal': 'inspect', 'status': 'in_progress', 'attempts': 1}},
        'current_step_id': 's1',
        'coder_messages': [{'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'tool-once', 'type': 'function', 'function': {'name': 'read_file', 'arguments': json.dumps({'path': 'a.txt'})}},
        ]}],
    })
    store.update_task(t['task_id'], state=state)
    store.append_event(t['task_id'], 'tool.started', {'tool_call_id': 'tool-once', 'tool': 'read_file', 'prepared': {}})
    store.append_event(t['task_id'], 'tool.completed', {'tool_call_id': 'tool-once', 'status': 'completed', 'output': 'already read'})

    class ResumeModel:
        def __init__(self): self.roles = []
        async def complete(self, messages, *, role, **kwargs):
            self.roles.append(role)
            if role == 'coder': return report('Read the file.')
            return LLMResponse(content=json.dumps({'action': 'respond', 'answer': 'The file was read.'}))

    model = ResumeModel()
    result = asyncio.run(AgentController(store, model, config).resume(t['task_id']))
    assert result['status'] == 'completed'
    assert model.roles == ['coder', 'planner']
    assert len([event for event in store.read_events(task_id=t['task_id']) if event['event_type'] == 'tool.started']) == 1
    assert any(event['payload'].get('reason') == 'recovery_tool_results' for event in store.read_events(task_id=t['task_id']) if event['event_type'] == 'node.transition')


def test_planner_closes_history_tool_when_transcript_is_complete(setup):
    repo, config, store = setup
    t = task(setup)

    class ToolOnly:
        calls = 0
        async def complete(self, messages, *, role, **kwargs):
            assert kwargs['tools'] is None
            self.calls += 1
            if self.calls == 2:
                assert 'complete session transcript' in json.dumps(messages)
                return planner_response(messages, {'action': 'respond', 'answer': 'Read from context.'})
            return LLMResponse(tool_calls=[ToolCall(id=f'history-{len(messages)}', name='read_session_messages', arguments={'after_seq': 0})])

    result = asyncio.run(AgentController(store, ToolOnly(), config).run(t['task_id']))
    assert result['status'] == 'completed', result['state'].get('error')
    events = store.read_events(task_id=t['task_id'])
    assert not any(e['event_type'] == 'planner.tool.started' for e in events)
    assert any(e['event_type'] == 'planner.tool.rejected' for e in events)


def test_planner_multi_tool_call_is_one_assistant_message_and_paginates(setup):
    repo, config, store = setup
    t = task(setup)
    store.append_session_event(t['session_id'], 'user.message', {'content': 'second turn', 'intent': 'auto'}, task_id=t['task_id'])
    seed_older_transcript(store, t)

    class MultiTool:
        def __init__(self): self.calls = 0
        async def complete(self, messages, *, role, **kwargs):
            if messages[0]['content'].startswith('Create a concise durable summary'):
                return LLMResponse(content='Earlier progress.')
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(tool_calls=[
                    ToolCall(id='page-1', name='read_session_messages', arguments={'after_seq': 0, 'limit': 1}),
                    ToolCall(id='page-2', name='read_session_messages', arguments={'after_seq': 1, 'limit': 1}),
                ])
            return planner_response(messages, {'action': 'respond', 'answer': 'I read both turns.'})

    result = asyncio.run(AgentController(store, MultiTool(), config).run(t['task_id']))
    assert result['status'] == 'completed'
    planner_messages = result['state']['planner_messages']
    assistant_calls = [message for message in planner_messages if message.get('tool_calls')]
    assert len(assistant_calls) == 1 and [call['id'] for call in assistant_calls[0]['tool_calls']] == ['page-1', 'page-2']
    outputs = [json.loads(message['content']) for message in planner_messages if message.get('role') == 'tool']
    assert outputs[0]['has_more'] is True
    assert outputs[0]['next_after_seq'] == outputs[0]['messages'][-1]['seq']
    assert outputs[1]['messages'][0]['seq'] > outputs[0]['next_after_seq']


@pytest.mark.parametrize('requested', ['verified_directory', 'repeated_listing', 'no_op', 'no_op_edit', 'new_check', 'fixed_check'])
def test_coder_preserves_new_checks_and_skips_redundant_calls_before_acceptance(setup, requested):
    repo, config, store = setup
    (repo / 'demo').mkdir()
    t = task(setup, [sys.executable, '-m', 'py_compile', 'demo/main.py'])

    class ModelWithRedundantInspection:
        coder_calls = 0
        async def complete(self, messages, *, role, **kwargs):
            if role == 'planner':
                context = json.loads(messages[-1]['content'].split('\n', 1)[1])
                if context['current_requirements']:
                    return LLMResponse(content='{"action":"respond","answer":"Delivered."}')
                decision = {'action': 'delegate', 'assignment': {'step_id': 's1', 'goal': 'Write demo'},
                            'requirements': {'source_user_seq': context['consumed_user_seq'], 'goal': 'Write demo',
                                             'delivery_targets': [
                                                 {'path': 'demo', 'kind': 'directory', 'operation': 'create'},
                                                 {'path': 'demo/main.py', 'kind': 'file', 'operation': 'create'},
                                             ]}}
                return LLMResponse(content=json.dumps(decision))
            self.coder_calls += 1
            check_command = [sys.executable, '-m', 'py_compile', 'demo/main.py']
            if requested == 'fixed_check':
                check_command = [sys.executable, '-c', 'import ast; from pathlib import Path; ast.parse(Path("demo/main.py").read_text())']
            calls = [
                ToolCall(id='write', name='edit_file', arguments={'path': 'demo/main.py', 'old_text': '', 'new_text': 'answer = 42\n'}),
                ToolCall(id='syntax', name='run_command', arguments={'command': check_command}),
            ]
            if requested not in ('verified_directory', 'no_op_edit'):
                calls.extend([
                    ToolCall(id='import', name='run_command', arguments={'command': [sys.executable, '-c', 'from demo.main import answer; assert answer == 42']}),
                    ToolCall(id='listing', name='run_command', arguments={'command': ['ls', '-la', 'demo']}),
                ])
            commands = {'verified_directory': ['ls', '-d', 'demo'],
                        'repeated_listing': ['ls', '-al', 'demo/main.py', 'demo'],
                        'no_op': ['true'],
                        'fixed_check': ['ls', '-al', 'demo/main.py', 'demo']}
            if requested == 'new_check':
                calls.append(ToolCall(id='new-check', name='run_tests', arguments={
                    'command': [sys.executable, '-c', 'from demo.main import answer; assert isinstance(answer, int)'],
                }))
            elif requested == 'no_op_edit':
                calls[1:] = [ToolCall(id='redundant', name='edit_file', arguments={
                    'path': 'demo/main.py', 'old_text': 'answer = 42', 'new_text': 'answer = 42',
                })]
            else:
                calls.append(ToolCall(id='redundant', name='run_command', arguments={'command': commands[requested]}))
            if self.coder_calls <= len(calls):
                assert kwargs['tools']
                return LLMResponse(tool_calls=[calls[self.coder_calls - 1]])
            if requested == 'new_check':
                assert kwargs['tools']
            else:
                assert kwargs['tools'] == []
                assert kwargs['response_format'] == {'type': 'json_object'}
            return report('Wrote demo and checked its syntax.')

    result = asyncio.run(AgentController(store, ModelWithRedundantInspection(), config).run(t['task_id']))
    assert result['status'] == 'completed', result['state'].get('error')
    events = store.read_events(task_id=t['task_id'])
    if requested not in ('verified_directory', 'no_op_edit'):
        assert any(e['event_type'] == 'tool.completed' and e['payload']['tool_call_id'] == 'import' for e in events)
    assert not any(e['event_type'] == 'tool.started' and e['payload']['tool_call_id'] == 'redundant' for e in events)
    milestone_types = ('coder.report', 'verification.result', 'assistant.final')
    if requested == 'new_check':
        assert any(e['event_type'] == 'tool.completed' and e['payload']['tool_call_id'] == 'new-check' for e in events)
        assert not any(e['event_type'] == 'coder.report.only_requested' for e in events)
    else:
        milestone_types = ('coder.report.only_requested', *milestone_types)
    assert [e['event_type'] for e in events if e['event_type'] in milestone_types] == list(milestone_types)


def test_planner_final_context_uses_durable_handoff_without_resummarizing_old_decision(setup):
    _, config, store = setup
    t = task(setup)

    class HandoffModel:
        async def complete(self, messages, *, role, **kwargs):
            assert not messages[0]['content'].startswith('Create a concise durable summary')
            if role == 'coder':
                return report('Inspected the project.')
            if 'Inspected the project.' in json.dumps(messages):
                return LLMResponse(content='{"action":"respond","answer":"Inspected."}')
            delegate = planner_response(messages, {'action': 'delegate', 'assignment': {'step_id': 's1', 'goal': 'Inspect the project'}})
            return LLMResponse(content=delegate.content + ' ' * 42000)

    result = asyncio.run(AgentController(store, HandoffModel(), config).run(t['task_id']))
    assert result['status'] == 'completed', result['state'].get('error')
    assert len(result['state']['planner_messages'][0]['content']) > 42000


def test_new_plan_cannot_claim_unobserved_completion_or_duplicate_ids(setup):
    repo, config, store = setup
    t = task(setup)
    runner = AgentController(store, Model([]), config)
    state = {'current_requirements': {'revision': 1, 'source_user_seq': 1, 'goal': 'old'},
             'plan': {'goal': 'old', 'steps': [{'id': 's1', 'goal': 'old work', 'status': 'completed'}]},
             'completed_step_ids': ['s1'], 'completed_step_goals': {'s1': 'old work'}}
    plan = TaskPlan.model_validate({'goal': 'new', 'steps': [
        {'id': 's1', 'goal': 'different work', 'status': 'completed'},
        {'id': 's2', 'goal': 'new work', 'status': 'completed'},
    ]})
    with pytest.raises(ValueError,match='already completed'):
        runner._install_plan_and_assignment(state, plan, ExecutionAssignment(step_id='s1', goal='different work'))
    runner._install_plan_and_assignment(state, plan, ExecutionAssignment(step_id='s2', goal='new work'))
    assert state['steps']['s1']['status'] == 'completed'
    assert state['steps']['s1']['goal'] == 'old work'
    assert state['steps']['s2']['status'] == 'in_progress'
    duplicate = TaskPlan.model_validate({'goal': 'bad', 'steps': [
        {'id': 'same', 'goal': 'one'}, {'id': 'same', 'goal': 'two'},
    ]})
    with pytest.raises(ValueError, match='unique'):
        runner._install_plan_and_assignment({}, duplicate, ExecutionAssignment(step_id='same', goal='one'))


def test_user_input_during_acceptance_check_is_consumed_before_repair_assignment(setup):
    repo, config, store = setup
    t = task(setup, [sys.executable, '-c', 'import time; time.sleep(.3); raise SystemExit(1)'])

    class RacingAcceptance:
        def __init__(self): self.planner_calls = 0; self.coder_calls = 0
        async def complete(self, messages, *, role, **kwargs):
            if role == 'planner':
                self.planner_calls += 1
                if self.planner_calls == 1:
                    return planner_response(messages, {'action': 'delegate', 'assignment': {'step_id': 's1', 'goal': 'implement'}})
                return planner_response(messages, {'action': 'respond', 'answer': 'The requested check failed.'})
            self.coder_calls += 1
            return report('Implementation complete.')

    model = RacingAcceptance()
    runner = AgentController(store, model, config)

    async def scenario():
        running = asyncio.create_task(runner.run(t['task_id']))
        for _ in range(200):
            await asyncio.sleep(.01)
            checks = [event for event in store.read_events(task_id=t['task_id']) if event['event_type'] == 'tool.started' and event['payload'].get('tool') == 'run_tests']
            if checks:
                store.append_session_event(t['session_id'], 'user.message', {
                    'content': 'Stop editing and explain the failure.', 'intent': 'redirect', 'target_task_id': t['task_id'],
                }, task_id=t['task_id'])
                break
        return await running

    result = asyncio.run(scenario())
    assert result['status'] == 'failed'
    assert result['state']['verification_blocked_by_input'] is True
    assert model.coder_calls == 1
    assert not any(event['event_type'] == 'assistant.final' for event in store.read_events(task_id=t['task_id']))


def test_read_file_segments_large_utf8_and_preserves_full_snapshot(setup):
    repo,config,store=setup;t=task(setup)
    original=''.join(f'第{i}行：你好，世界🌏\n' for i in range(1,251))
    (repo/'large.txt').write_text(original,encoding='utf-8')
    executor=ToolExecutor(store,config)

    first=asyncio.run(executor.execute(t,ToolCall(id='first',name='read_file',arguments={'path':'large.txt'})))
    assert first.output==''.join(f'第{i}行：你好，世界🌏\n' for i in range(1,201))
    assert first.metadata['total_lines']==250
    assert (first.metadata['line_start'],first.metadata['line_end'])==(1,200)
    assert first.metadata['truncated'] is True
    snapshot=store.get_artifact(first.metadata['artifact_id'])
    assert snapshot['kind']=='file_snapshot'
    assert Path(snapshot['path']).read_text(encoding='utf-8')==original

    last=asyncio.run(executor.execute(t,ToolCall(id='last',name='read_file',arguments={
        'path':'large.txt','start_line':201,'max_lines':50,
    })))
    assert last.output==''.join(f'第{i}行：你好，世界🌏\n' for i in range(201,251))
    assert (last.metadata['line_start'],last.metadata['line_end'])==(201,250)
    assert last.metadata['truncated'] is True

    out_of_range=asyncio.run(executor.execute(t,ToolCall(id='past-end',name='read_file',arguments={
        'path':'large.txt','start_line':251,
    })))
    assert out_of_range.status=='completed'
    assert 'exceeds file length (250 lines)' in out_of_range.output
    assert (out_of_range.metadata['line_start'],out_of_range.metadata['line_end'])==(0,0)

    invalid=asyncio.run(executor.execute(t,ToolCall(id='bool-line',name='read_file',arguments={
        'path':'large.txt','start_line':True,
    })))
    assert invalid.status=='failed'
    assert invalid.output=='start_line must be a positive integer'


def test_small_read_file_output_remains_unchanged(setup):
    repo,config,store=setup;t=task(setup)
    source='alpha\nbeta\n'
    (repo/'small.txt').write_text(source)
    result=asyncio.run(ToolExecutor(store,config).execute(
        t,ToolCall(id='small',name='read_file',arguments={'path':'small.txt'})
    ))
    assert result.output==source
    assert result.metadata['truncated'] is False
    assert (result.metadata['line_start'],result.metadata['line_end'],result.metadata['total_lines'])==(1,2,2)


