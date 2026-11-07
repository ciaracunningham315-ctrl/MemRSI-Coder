import asyncio
import json
from pathlib import Path

import typer

from evocode.app import Application
from evocode.config import load_config
from evocode.contracts import SessionMessageSpec, TaskSpec
from evocode.llm import doctor as check

app = typer.Typer(help='EvoCode coding agent')
tasks = typer.Typer(); app.add_typer(tasks, name='task')
memory = typer.Typer(); app.add_typer(memory, name='memory')
skills = typer.Typer(); app.add_typer(skills, name='skills')
evolution = typer.Typer(); app.add_typer(evolution, name='evolution')
sessions = typer.Typer(); app.add_typer(sessions, name='sessions')
def show(value): typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))
async def use(fn):
    svc = Application(load_config())
    try: return await fn(svc)
    finally: await svc.close()
@app.command()
def doctor(): show(asyncio.run(check(load_config())))
@app.command()
def serve(host: str='127.0.0.1', port: int=8787):
    import uvicorn
    uvicorn.run('evocode.api:create_app', factory=True, host=host, port=port)
@tasks.command('run')
def run(repo: str=typer.Option(...), prompt: str=typer.Option(...), acceptance: str=''):
    import shlex
    async def work(s):
        task = s.create_task(TaskSpec(repo_path=repo,prompt=prompt,acceptance_command=shlex.split(acceptance)))
        typer.echo('task_id: '+task['task_id'])
        return await s.run(task['task_id'])
    show(asyncio.run(use(work)))
@tasks.command('resume')
def resume(task: str=typer.Option(...)): show(asyncio.run(use(lambda s: s.run(task, resume=True))))
@tasks.command('list')
def task_list():
    async def work(s): return s.store.list_tasks()
    show(asyncio.run(use(work)))
@tasks.command('show')
def task_show(task: str):
    async def work(s): return {'task':s.store.get_task(task),'events':s.store.read_events(task_id=task)}
    show(asyncio.run(use(work)))
@tasks.command('resolve')
def resolve(task: str=typer.Option(...), call: str=typer.Option(...), resolution: str=typer.Option(...)):
    async def work(s): return s.controller.resolve(task, call, resolution)
    show(asyncio.run(use(work)))
@memory.command('status')
def memory_status():
    async def work(s): return s.memory.status()
    show(asyncio.run(use(work)))
@memory.command('rebuild')
def memory_rebuild(): show(asyncio.run(use(lambda s:s.memory.rebuild_index())))
@skills.command('list')
def skill_list():
    async def work(s): return s.skills.list_skills()
    show(asyncio.run(use(work)))
@skills.command('show')
def skill_show(version: str):
    async def work(s): return s.skills.load(version)
    show(asyncio.run(use(work)))
@evolution.command('trigger')
def evolve(task: str='', kind: str='task'):
    show(asyncio.run(use(lambda s:s.evolution.trigger(task or None,kind=kind,manual=True))))
@evolution.command('evaluate')
def evaluate(run: str=typer.Option(...), specs: Path=typer.Option(...)):
    show(asyncio.run(use(lambda s:s.evolution.evaluate(run,json.loads(specs.read_text())))))
@evolution.command('publish')
def publish(run: str=typer.Option(...)):
    async def work(s): return s.evolution.publish(run)
    show(asyncio.run(use(work)))
@evolution.command('rollback')
def rollback_merge(run: str=typer.Option(...)):
    """Roll back a published merge and restore its source skills."""
    async def work(s): return s.evolution.rollback_merge(run)
    show(asyncio.run(use(work)))
@sessions.command('export')
def export(session: str=typer.Option(...), output: Path=typer.Option(...)):
    async def work(s): return s.store.read_events(session_id=session)
    rows=asyncio.run(use(work)); output.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    typer.echo(str(output))

@sessions.command('create')
def session_create(repo: str=typer.Option(...), title: str=''):
    async def work(s): return s.create_session(repo,title)
    show(asyncio.run(use(work)))

@sessions.command('show')
def session_show(session: str=typer.Option(...)):
    async def work(s): return s.store.get_session(session)
    show(asyncio.run(use(work)))

@sessions.command('rebuild')
def session_rebuild():
    """Rebuild session and task state from SQLite events. Stop the service first."""
    async def work(s): return s.store.rebuild_materialized_state()
    show(asyncio.run(use(work)))

@sessions.command('send')
def session_send(session: str=typer.Option(...), prompt: str=typer.Option(...), acceptance: str=''):
    """Continue a conversation; the application chooses or resumes its execution."""
    async def work(s):
        receipt=await s.add_session_message(session,SessionMessageSpec(content=prompt,acceptance_command=acceptance))
        job=s.jobs.get(receipt['task_id'])
        if job:
            await job
        return s.store.get_session(session)
    show(asyncio.run(use(work)))

@evolution.command('status')
def evolution_status():
    async def work(s): return s.evolution.list_runs()
    show(asyncio.run(use(work)))

def set_memory_enabled(enabled):
    import yaml
    cfg=load_config();cfg.raw_rag.enabled=enabled
    path=Path('config/local.yaml');path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(cfg.snapshot(),allow_unicode=True))
    show({'enabled':enabled,'effective':'next_task; restart an already running server or change its settings via API'})
@memory.command('enable')
def memory_enable():set_memory_enabled(True)
@memory.command('disable')
def memory_disable():set_memory_enabled(False)
