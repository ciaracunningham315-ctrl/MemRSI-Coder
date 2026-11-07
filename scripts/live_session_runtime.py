"""Small, metered DeepSeek/Docker acceptance run; never changes local.yaml.

Run from the repository root: .venv/bin/python scripts/live_session_runtime.py
All source files, sessions and events belong to a separate data/runtime-checks run.
"""
import argparse
import asyncio
import json
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from evocode.app import Application
from evocode.config import load_config
from evocode.contracts import SessionMessageSpec
from evocode.llm import ModelClient


class Meter:
    def __init__(self):
        self.requests=[]
        self.total_tokens=0
        self.max_requests=18
        self.max_tokens=70000


class MeteredClient:
    def __init__(self,config,meter):
        self.client=ModelClient(config)
        self.meter=meter

    def for_config(self,config):
        return MeteredClient(config,self.meter)

    async def complete(self,messages,**kwargs):
        meter=self.meter
        estimate=len(json.dumps({'messages':messages,**kwargs},ensure_ascii=False).encode())+kwargs.get('max_tokens',1536)+512
        if len(meter.requests)>=meter.max_requests or meter.total_tokens+estimate>meter.max_tokens:
            raise RuntimeError('Live-check request/token cap reached; stop further paid calls')
        entry={'role':kwargs.get('role','coder'),'status':'started'}
        meter.requests.append(entry)
        print(f"request {len(meter.requests)}/{meter.max_requests}: {entry['role']}",flush=True)
        started=time.monotonic()
        try:
            result=await self.client.complete(messages,**kwargs)
        except BaseException as exc:
            entry.update(status='failed',error=type(exc).__name__)
            raise
        entry.update(status='completed',model=result.model,usage=result.usage,
                     seconds=round(time.monotonic()-started,3))
        meter.total_tokens+=result.usage.get('total_tokens',0)
        return result


async def main(skip_greetings=False):
    root=(Path('data/runtime-checks')/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])).resolve()
    workspace=root/'workspace'
    (workspace/'tests').mkdir(parents=True)
    (workspace/'calc.py').write_text('def add(a, b):\n    return a - b\n')
    test_source=('import unittest\nfrom calc import add\n'
                 'class TestAdd(unittest.TestCase):\n'
                 '    def test_positive(self): self.assertEqual(add(2, 3), 5)\n'
                 '    def test_negative(self): self.assertEqual(add(-1, -2), -3)\n')
    (workspace/'tests/test_calc.py').write_text(test_source)
    cfg=load_config()
    cfg.data_dir=str(root/'store')
    cfg.models.local=cfg.models.evolution.model_copy(update={
        'model_id':'deepseek-flash','thinking':'disabled','max_output_tokens':1536,'timeout':90})
    cfg.runtime.execution_backend='docker'
    cfg.runtime.max_turns=10
    cfg.runtime.max_replans=2
    cfg.runtime.planner_context_tokens=16000
    cfg.raw_rag.enabled=False
    cfg.raw_rag.index_when_disabled=False
    cfg.raw_rag.background_indexing=False
    cfg.skills.enabled=False
    cfg.evolution.enabled=False
    meter=Meter()
    if skip_greetings:
        meter.max_requests=12
        previous=json.loads(Path('docs/deepseek-session-runtime-first-attempt.json').read_text())
        meter.max_tokens=max(1,70000-previous['total_tokens'])
    app=Application(cfg,MeteredClient(cfg,meter))
    path=Path('docs/deepseek-session-runtime-result.json')
    report={'date':datetime.now(UTC).isoformat(),'model':cfg.models.local.model_id,
            'thinking':'disabled','execution_backend':'docker','data_dir':cfg.data_dir,
            'workspace':str(workspace),'request_limit':meter.max_requests,'token_limit':meter.max_tokens,
            'remote_embedding_reranker_evolution_calls':0,'status':'running','scenarios':[]}

    def save():
        report.update(requests=meter.requests,total_tokens=meter.total_tokens)
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2))

    async def send(sid,text,command=''):
        receipt=await app.add_session_message(sid,SessionMessageSpec(
            content=text,acceptance_command=command,client_message_id=str(uuid.uuid4())))
        result=await app.jobs[receipt['task_id']]
        return result

    def collect(name,result,checks):
        events=app.store.read_events(task_id=result['task_id'])
        finals=[e for e in events if e['event_type']=='assistant.final']
        checks['one_final']=len(finals)==1 if result['status']=='completed' else len(finals)==0
        passed=all(checks.values())
        record={'name':name,'task_id':result['task_id'],'session_id':result['session_id'],
                'status':result['status'],'answer':result['state'].get('final_answer'),
                'error':result['state'].get('error'),'checks':checks,'passed':passed,
                'verification':result['state'].get('verification'),
                'tokens':result['state'].get('tokens',0),
                'coder_turns':result['state'].get('coder_turns',0),
                'path':[e['payload'] for e in events if e['event_type']=='node.transition']}
        report['scenarios'].append(record)
        save()
        print(json.dumps({'scenario':name,'passed':passed,'status':result['status'],'answer':record['answer'],'error':record['error']},ensure_ascii=False),flush=True)
        if not passed:
            raise RuntimeError('Live scenario failed: '+name)

    try:
        sid=app.create_session(str(workspace),'DeepSeek 连续对话实测')['session']['session_id']
        if not skip_greetings:
            for number in (1,2):
                result=await send(sid,'你好')
                collect(f'greeting_{number}',result,{'completed':result['status']=='completed',
                        'planner_only':result['state'].get('coder_turns',0)==0})
            result=await send(sid,'我在本会话里独立发过几次“你好”？只统计完整内容为“你好”的用户消息，不算本条问题。')
            collect('session_history_count',result,{'completed':result['status']=='completed',
                    'count_is_two':bool(re.search(r'2|两|二',result['state'].get('final_answer',''))),
                    'raw_rag_disabled':not cfg.raw_rag.enabled})

        result=await send(sid,'请把以下工作作为一个执行目标处理：读取 calc.py，修复 add(a,b) 的减法错误为加法，运行 python -m unittest discover -s tests。只能修改 calc.py，禁止修改测试。完成后简短报告。',
                          ['python','-m','unittest','discover','-s','tests'])
        collect('coder_docker_edit_acceptance',result,{'completed':result['status']=='completed',
                'actual_edit':'+ b' in (workspace/'calc.py').read_text(),
                'tests_unchanged':(workspace/'tests/test_calc.py').read_text()==test_source,
                'docker_acceptance_passed':result['state'].get('verification',{}).get('status')=='passed',
                'coder_used':result['state'].get('coder_turns',0)>0})

        sid=app.create_session(str(workspace),'等待用户实测')['session']['session_id']
        question=await send(sid,'请先问我选择 A 还是 B。收到选择前不要代选，也不要读取或修改代码。')
        collect('ask_user',question,{'waiting_user':question['status']=='waiting_user',
                'question_saved':any(m['kind']=='question' for m in app.store.read_conversation_messages(sid))})
        result=await send(sid,'选择 A。只需简短确认，不要操作代码。')
        collect('resume_waiting_user',result,{'completed':result['status']=='completed',
                'same_task':result['task_id']==question['task_id'],'confirmed_A':'A' in result['state'].get('final_answer','')})

        sid=app.create_session(str(workspace),'运行中纠正实测')['session']['session_id']
        receipt=await app.add_session_message(sid,SessionMessageSpec(content='请读取 calc.py，说明这个函数的作用。'))
        tid=receipt['task_id']
        for _ in range(300):
            if any(e['event_type']=='context.assembled' for e in app.store.read_events(task_id=tid)):
                break
            await asyncio.sleep(.01)
        else:
            raise RuntimeError('No in-flight Planner context observed')
        correction=await app.add_session_message(sid,SessionMessageSpec(content='更正：不要读取或修改任何文件，直接简短回复“已改为只解释”，然后结束。'))
        result=await app.jobs[tid]
        events=app.store.read_events(task_id=tid)
        collect('inflight_steering',result,{'completed':result['status']=='completed',
                'same_task':correction['task_id']==tid,
                'no_old_tools_executed':not any(e['event_type']=='tool.started' for e in events),
                'latest_request_followed':'已改为只解释' in result['state'].get('final_answer','')})
        report['status']='passed'
    except BaseException as exc:
        report.update(status='failed',error=type(exc).__name__+': '+str(exc))
        raise
    finally:
        save()
        await app.close()
        print(json.dumps({'report':str(path),'status':report['status'],'requests':len(meter.requests),
                          'total_tokens':meter.total_tokens},ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--skip-greetings',action='store_true',help='Reuse the recorded greeting/history success and spend only the remaining token budget')
    args=parser.parse_args()
    asyncio.run(main(args.skip_greetings))
