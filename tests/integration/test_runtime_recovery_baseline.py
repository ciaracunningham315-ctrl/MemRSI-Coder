"""Restart recovery coverage for interrupted tools and later user corrections."""
import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

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
    cfg = AppConfig(data_dir=str(tmp_path / 'data'))
    cfg.raw_rag.enabled = False
    cfg.raw_rag.background_indexing = False
    cfg.skills.enabled = False
    cfg.evolution.enabled = False
    cfg.runtime.execution_backend = 'host'
    return cfg


class CorrectedPlanner:
    def __init__(self):
        self.contexts = []

    async def complete(self, messages, *, role='planner', **kwargs):
        assert role == 'planner'
        context = json.loads(messages[-1]['content'].split('\n', 1)[1])
        self.contexts.append(context)
        current = context['current_requirements']
        return LLMResponse(content=json.dumps({
            'action': 'respond',
            'answer': '已根据修正后的要求继续处理。',
            'requirements': {
                'expected_revision': current['revision'] if current else 0,
                'source_user_seq': context['consumed_user_seq'],
                'goal': context['latest_user_requirement'],
            },
        }, ensure_ascii=False))


async def test_restart_keeps_uncertain_command_blocked_then_applies_correction_without_replay(tmp_path):
    cfg = configuration(tmp_path)
    repo = tmp_path / 'repo'
    repo.mkdir()
    first = Application(cfg, CorrectedPlanner())
    session_id = first.create_session(str(repo))['session']['session_id']
    task = first.create_task(TaskSpec(
        repo_path=str(repo), prompt='Run the maintenance command', session_id=session_id,
    ))
    task_id = task['task_id']

    # Model the durable boundary after a command may have begun, before its result
    # was recorded. The command itself must stay uncertain through recovery.
    state = dict(task['state'])
    state.update({
        'last_user_event_seq': 1,
        'next_node': 'tool',
        'coder_messages': [{
            'role': 'assistant', 'content': '',
            'tool_calls': [{
                'id': 'maintenance-command',
                'type': 'function',
                'function': {
                    'name': 'run_command',
                    'arguments': json.dumps({'command': ['sh', '-c', 'touch replayed'] }),
                },
            }],
        }],
    })
    first.store.update_task(task_id, status='needs_review', state=state)
    first.store.append_event(task_id, 'tool.started', {
        'tool_call_id': 'maintenance-command',
        'tool': 'run_command',
        'arguments': {'command': ['sh', '-c', 'touch replayed']},
        'prepared': {},
    })

    correction = await first.add_session_message(
        session_id, SessionMessageSpec(content='不要运行维护命令，只说明当前状态。'),
    )
    assert correction['task_id'] == task_id
    assert correction['blocked'] is True
    assert correction['queued'] is False
    await first.close()

    model = CorrectedPlanner()
    restarted = Application(cfg, model)
    try:
        still_uncertain = await restarted.run(task_id, resume=True)
        assert still_uncertain['status'] == 'needs_review'
        assert 'maintenance-command' in restarted.controller._uncertain(task_id)
        assert not (repo / 'replayed').exists()

        restarted.controller.resolve(task_id, 'maintenance-command', 'failed')
        finished = await restarted.run(task_id, resume=True)

        assert finished['status'] == 'completed', finished['state'].get('error')
        assert model.contexts
        assert model.contexts[-1]['latest_user_requirement'] == '不要运行维护命令，只说明当前状态。'
        events = restarted.store.read_events(task_id=task_id)
        assert len([event for event in events if event['event_type'] == 'tool.started'
                    and event['payload'].get('tool_call_id') == 'maintenance-command']) == 1
        assert not (repo / 'replayed').exists()
        assert [message['content'] for message in restarted.store.get_session(session_id)['messages']
                if message['kind'] == 'final'] == ['已根据修正后的要求继续处理。']
    finally:
        await restarted.close()


@pytest.mark.skipif(
    os.getenv('EVOCODE_DOCKER_TESTS') != '1',
    reason='set EVOCODE_DOCKER_TESTS=1 to run the Docker cancellation check',
)
async def test_stopping_docker_command_removes_container_and_preserves_uncertainty(tmp_path):
    if not shutil.which('docker'):
        pytest.skip('Docker CLI is unavailable')
    image = 'python:3.12-slim'
    available = subprocess.run(
        ['docker', 'image', 'inspect', image], capture_output=True, text=True,
    )
    if available.returncode:
        pytest.skip(f'{image} is not present locally; test will not pull images')

    cfg = configuration(tmp_path)
    cfg.runtime.execution_backend = 'docker'
    cfg.runtime.docker_image = image
    repo = tmp_path / 'repo'
    repo.mkdir()
    start_marker = repo / 'command-started'
    finish_marker = repo / 'command-finished'
    app = Application(cfg, object())
    task = app.create_task(TaskSpec(repo_path=str(repo), prompt='run a long command'))
    task_id = task['task_id']

    async def plan(*args, **kwargs):
        return PlannerDecision(
            action='delegate',
            requirements=RequirementsProposal(source_user_seq=1, goal='run a long command'),
            plan=TaskPlan(goal='run a long command', steps=[{'id': 'step1', 'goal': 'run a long command'}]),
            assignment=ExecutionAssignment(step_id='step1', goal='run a long command'),
        )

    async def coder(*args, **kwargs):
        code = (
            "from pathlib import Path; import time; "
            f"Path({str(Path('/workspace') / start_marker.name)!r}).write_text('started'); "
            "time.sleep(30); "
            f"Path({str(Path('/workspace') / finish_marker.name)!r}).write_text('finished')"
        )
        return LLMResponse(tool_calls=[ToolCall(
            id='docker-long-command', name='run_command',
            arguments={'command': ['python', '-c', code]},
        )])

    app.controller._plan = plan
    app.controller.coder.complete = coder
    try:
        app.schedule(task_id, app.run(task_id))
        container = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            executor = app.controller._executors.get(task_id)
            if executor:
                container = executor.containers.get(task_id)
            if start_marker.exists() and container:
                break
            if app.jobs[task_id].done():
                pytest.fail(f'Agent stopped before the Docker command started: {app.jobs[task_id].result()}')
            await asyncio.sleep(.05)
        assert start_marker.exists(), 'Docker command did not write its start marker'
        assert container, 'Docker container was not registered during execution'

        stopped = await app.stop_task(task_id)
        assert stopped['status'] == 'needs_review'
        assert stopped['state']['uncertain_tools'][0]['tool_call_id'] == 'docker-long-command'
        assert not finish_marker.exists()
        inspect = subprocess.run(['docker', 'inspect', container], capture_output=True, text=True)
        assert inspect.returncode != 0, f'Docker container {container} still exists after stop'
    finally:
        await app.close()
