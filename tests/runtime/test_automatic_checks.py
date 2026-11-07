import asyncio
import json
import os
import py_compile

import pytest

from evocode.agent import AgentController
from evocode.agent.checks import ProjectCheckDiscovery
from evocode.agent.tools import ToolExecutor
from evocode.agent.verification import AcceptanceVerifier
from evocode.config import AppConfig
from evocode.contracts import LLMResponse, TaskSpec, ToolCall, ToolResult
from evocode.persistence import EventStore


@pytest.fixture
def project(tmp_path):
    repo=tmp_path/'repo';repo.mkdir()
    cfg=AppConfig(data_dir=str(tmp_path/'data'))
    cfg.runtime.execution_backend='host';cfg.raw_rag.enabled=False;cfg.skills.enabled=False
    store=EventStore(cfg.sqlite_path)
    yield repo,cfg,store
    store.close()


def unit_project(repo):
    (repo/'calc.py').write_text('def add(a,b): return a-b\n')
    (repo/'tests').mkdir()
    (repo/'tests/test_calc.py').write_text('import unittest\nfrom calc import add\nclass TestAdd(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n')


def test_unittest_discovery_and_ai_selected_check_run_without_user_command(project):
    repo,cfg,store=project;unit_project(repo)
    task=store.create_task(TaskSpec(repo_path=str(repo),prompt='修复加法'),cfg.snapshot())
    class Model:
        def __init__(self):self.coders=0;self.planners=0
        async def complete(self,messages,*,role,**kwargs):
            if role=='planner':
                self.planners+=1
                context=json.loads(messages[-1]['content'].split('\n',1)[1])
                if self.planners==1:
                    check=context['project_checks'][0]
                    return LLMResponse(content=json.dumps({'action':'delegate','check_ids':[check['id']],'requirements':{'source_user_seq':context['consumed_user_seq'],'goal':'fix add'},'assignment':{'step_id':'s1','goal':'fix add'}}))
                if self.planners==3:
                    assert context['verification']['status']=='passed'
                return LLMResponse(content='{"action":"respond","answer":"已修复并检查。"}')
            self.coders+=1
            if self.coders==1:
                return LLMResponse(tool_calls=[ToolCall(id='edit',name='edit_file',arguments={'path':'calc.py','old_text':'a-b','new_text':'a+b'})])
            return LLMResponse(content='{"step_id":"s1","outcome":"completed","summary":"修改了加法","changed_files":["calc.py"]}')
    result=asyncio.run(AgentController(store,Model(),cfg).run(task['task_id']))
    assert result['status']=='completed',result
    assert result['state']['acceptance_command']==[]
    verification=result['state']['verification']
    assert verification['source']=='automatic' and verification['status']=='passed'
    assert verification['checks'][0]['check']['command'][1:]==['-m','unittest','discover','-s','tests']
    assert 'Ran 1 test' in verification['checks'][0]['result']['output']
    assert len([e for e in store.read_events(task_id=task['task_id']) if e['event_type']=='assistant.final'])==1


def test_automatic_failed_check_enters_repair_then_verifies_again(project):
    repo,cfg,store=project;unit_project(repo)
    task=store.create_task(TaskSpec(repo_path=str(repo),prompt='fix'),cfg.snapshot())
    class Repair:
        def __init__(self):self.coders=0
        async def complete(self,messages,*,role,**kwargs):
            if role=='planner':
                return LLMResponse(content='{"action":"delegate","requirements":{"source_user_seq":1,"goal":"fix"},"assignment":{"step_id":"s1","goal":"fix"}}' if self.coders==0 else '{"action":"respond","answer":"已处理"}')
            self.coders+=1
            if self.coders==2:
                return LLMResponse(tool_calls=[ToolCall(id='repair',name='edit_file',arguments={'path':'calc.py','old_text':'a-b','new_text':'a+b'})])
            return LLMResponse(content='{"step_id":"s1","outcome":"completed","summary":"done","changed_files":["calc.py"]}')
    result=asyncio.run(AgentController(store,Repair(),cfg).run(task['task_id']))
    assert result['status']=='completed',result
    assert result['state']['verification_attempts']==1
    assert result['state']['verification']['status']=='passed'
    assert result['state']['acceptance_command']==[]
    results=[e['payload']['status'] for e in store.read_events(task_id=task['task_id']) if e['event_type']=='verification.result']
    assert results==['failed','passed']


def test_node_subproject_detection_uses_lockfile_and_run_mode(tmp_path):
    web=tmp_path/'web';web.mkdir()
    (web/'package.json').write_text(json.dumps({'scripts':{'test':'vitest','build':'vite build','typecheck':'tsc --noEmit'}}))
    (web/'pnpm-lock.yaml').write_text('lockfileVersion: 9')
    checks=ProjectCheckDiscovery().discover(tmp_path,['web/src/a.ts'])
    test=next(check for check in checks if check.kind=='test')
    assert test.cwd=='web' and test.command==['pnpm','run','test','--run']
    assert test.source=='web/package.json:scripts.test'
    assert len(ProjectCheckDiscovery.select(checks,['web/src/a.ts']))==3


def test_placeholder_and_watch_commands_are_not_automatically_selected(tmp_path):
    (tmp_path/'package.json').write_text(json.dumps({'scripts':{'test':'echo "Error: no test specified" && exit 1','test:unit':'vitest --watch'}}))
    assert ProjectCheckDiscovery().discover(tmp_path)==[]


def test_python_without_tests_gets_syntax_only_check(project):
    repo,cfg,store=project;(repo/'a.py').write_text('answer = 42\n')
    task=store.create_task(TaskSpec(repo_path=str(repo),prompt='fix'),cfg.snapshot())
    state=task['state'];state['last_coder_report']={'changed_files':['a.py']}
    verifier=AcceptanceVerifier(store)
    class Runner:
        def __init__(self):self.store=store
        async def _execute(self,task,state,executor,call):return await executor.execute(task,call)
    result=asyncio.run(verifier.verify(Runner(),task,state,ToolExecutor(store,cfg),1))
    assert result['status']=='passed' and result['coverage']=='syntax_only'
    assert 'no functional tests were run' in result['result']['output']


def test_missing_test_dependency_is_blocked_and_never_passed(project):
    repo,cfg,store=project
    (repo/'pyproject.toml').write_text('[tool.pytest.ini_options]\ntestpaths=["tests"]\n')
    task=store.create_task(TaskSpec(repo_path=str(repo),prompt='fix'),cfg.snapshot())
    class MissingDependency:
        def __init__(self):self.store=store
        async def _execute(self,task,state,executor,call):
            return ToolResult(tool_call_id=call.id,status='failed',exit_code=1,output='No module named pytest')
    result=asyncio.run(AcceptanceVerifier(store).verify(MissingDependency(),task,task['state'],None,1))
    assert result['status']=='blocked'
    assert result['checks'][0]['status']=='blocked'


def test_empty_project_has_no_fabricated_check_result(project):
    repo,cfg,store=project
    task=store.create_task(TaskSpec(repo_path=str(repo),prompt='fix'),cfg.snapshot())
    class NoExecution:
        def __init__(self):self.store=store
        async def _execute(self,*args):raise AssertionError('No check exists')
    result=asyncio.run(AcceptanceVerifier(store).verify(NoExecution(),task,task['state'],None,1))
    assert result['status']=='unknown' and result['checks']==[]


def test_functional_missing_file_failure_is_not_misclassified_as_environment(project):
    repo,cfg,store=project;unit_project(repo)
    task=store.create_task(TaskSpec(repo_path=str(repo),prompt='fix'),cfg.snapshot())
    class FailedTest:
        def __init__(self):self.store=store
        async def _execute(self,task,state,executor,call):
            return ToolResult(tool_call_id=call.id,status='failed',exit_code=1,
                output="Traceback: FileNotFoundError: [Errno 2] No such file or directory: 'required-data.json'\nFAILED (errors=1)")
    result=asyncio.run(AcceptanceVerifier(store).verify(FailedTest(),task,task['state'],None,1))
    assert result['status']=='failed'


def test_nested_command_working_directory_is_confined(project):
    repo,cfg,store=project;(repo/'sub').mkdir();(repo/'sub/file.txt').write_text('inside')
    task=store.create_task(TaskSpec(repo_path=str(repo),prompt='inspect'),cfg.snapshot())
    executor=ToolExecutor(store,cfg)
    import sys
    result=asyncio.run(executor.execute(task,ToolCall(id='cwd',name='run_tests',arguments={'cwd':'sub','command':[sys.executable,'-c','from pathlib import Path; print(Path("file.txt").read_text())']})))
    assert result.status=='completed' and result.output.strip()=='inside'
    outside=asyncio.run(executor.execute(task,ToolCall(id='escape',name='run_tests',arguments={'cwd':'../','command':[sys.executable,'-c','print("bad")']})))
    assert outside.status=='failed'


def test_discovery_does_not_read_configs_through_external_symlinks(tmp_path):
    repo=tmp_path/'repo';repo.mkdir();outside=tmp_path/'outside';outside.mkdir()
    (outside/'package.json').write_text('{"scripts":{"test":"node sensitive.js"}}')
    (repo/'package.json').symlink_to(outside/'package.json')
    assert ProjectCheckDiscovery().discover(repo)==[]


@pytest.mark.parametrize('backend',['host','docker'])
def test_checks_read_latest_source_even_when_old_bytecode_looks_current(project,backend):
    if backend=='docker' and os.getenv('EVOCODE_DOCKER_TESTS')!='1':
        pytest.skip('explicit Docker integration opt-in')
    repo,cfg,store=project
    source=repo/'value.py';source.write_text('answer = 1\n')
    compiled=py_compile.compile(str(source),doraise=True)
    before=source.stat()
    source.write_text('answer = 2\n')
    os.utime(source,ns=(before.st_atime_ns,before.st_mtime_ns))
    import sys
    cfg.runtime.execution_backend=backend
    task=store.create_task(TaskSpec(repo_path=str(repo),prompt='check'),cfg.snapshot())
    command=['python' if backend=='docker' else sys.executable,'-c','from value import answer; print(answer); assert answer == 2']
    result=asyncio.run(ToolExecutor(store,cfg).execute(task,ToolCall(id='fresh',name='run_tests',arguments={'command':command})))
    assert result.status=='completed',result
    assert result.output.strip()=='2'
    assert os.path.exists(compiled)
