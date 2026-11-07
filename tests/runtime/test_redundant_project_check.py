import asyncio
import sys

import pytest

from evocode.agent import AgentController
from evocode.agent.execution_stop import redundant_inspections
from evocode.agent.tools import ToolExecutor
from evocode.agent.verification import AcceptanceVerifier
from evocode.config import AppConfig
from evocode.contracts import LLMResponse, PlannerDecision, TaskSpec, ToolCall
from evocode.persistence import EventStore


@pytest.fixture
def completed_project_check(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'tests').mkdir()
    (repo / 'sub' / 'tests').mkdir(parents=True)
    delivered = repo / 'app.py'
    delivered.write_text('answer = 42\n')

    config = AppConfig(data_dir=str(tmp_path / 'data'))
    config.runtime.execution_backend = 'host'
    store = EventStore(config.sqlite_path)
    task = store.create_task(TaskSpec(repo_path=str(repo), prompt='write app.py'), config.snapshot())
    state = dict(task['state'])
    state['current_requirements'] = {
        'revision': 1,
        'source_user_seq': 1,
        'goal': 'Write app.py',
        'delivery_required': True,
        'delivery_targets': [{'path': 'app.py', 'kind': 'file', 'operation': 'create'}],
    }
    store.append_event(task['task_id'], 'plan.step.started', {'step_id': 'step1'})
    store.append_event(task['task_id'], 'tool.started', {
        'tool_call_id': 'edit-app', 'tool': 'edit_file',
        'arguments': {'path': 'app.py', 'old_text': '', 'new_text': 'answer = 42\n'},
    })
    store.append_event(task['task_id'], 'tool.completed', {
        'tool_call_id': 'edit-app', 'status': 'completed', 'file_paths': ['app.py'],
    })
    store.append_event(task['task_id'], 'tool.started', {
        'tool_call_id': 'check-1', 'tool': 'run_tests',
        'arguments': {
            'command': ['python', '-m', 'unittest', 'discover', '-s', 'tests'],
            'cwd': '.',
        },
    })
    store.append_event(task['task_id'], 'tool.completed', {
        'tool_call_id': 'check-1', 'status': 'completed', 'exit_code': 0,
    })
    yield task, state, store, ToolExecutor(store, config), AcceptanceVerifier(store)
    store.close()


@pytest.mark.parametrize('tool_name', ['run_command', 'run_tests'])
def test_verbose_explicit_default_pattern_matches_across_tool_names(completed_project_check, tool_name):
    task, state, store, executor, verifier = completed_project_check
    repeated = ToolCall(id='check-2', name=tool_name, arguments={
        'command': ['python', '-m', 'unittest', 'discover', '-v', '-s', 'tests', '-p', 'test*.py'],
        'cwd': '.',
    })

    assert redundant_inspections(store, verifier, task, state, executor, 'step1', [repeated])


def test_different_cwd_does_not_match_a_successful_discovery(completed_project_check):
    task, state, store, executor, verifier = completed_project_check
    repeated = ToolCall(id='check-2', name='run_command', arguments={
        'command': ['python', '-m', 'unittest', 'discover', '-v', '-s', 'tests'],
        'cwd': 'sub',
    })

    assert not redundant_inspections(store, verifier, task, state, executor, 'step1', [repeated])


@pytest.mark.parametrize('executable', ['python3', '/usr/bin/python'])
def test_different_python_executable_does_not_match(completed_project_check, executable):
    task, state, store, executor, verifier = completed_project_check
    repeated = ToolCall(id='check-2', name='run_command', arguments={
        'command': [executable, '-m', 'unittest', 'discover', '-v', '-s', 'tests'],
        'cwd': '.',
    })

    assert not redundant_inspections(store, verifier, task, state, executor, 'step1', [repeated])


@pytest.mark.parametrize('command', [
    ['python', '-m', 'unittest', 'discover', '-q', '-s', 'tests'],
    ['python', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'],
])
def test_unknown_flags_or_patterns_fail_closed(completed_project_check, command):
    task, state, store, executor, verifier = completed_project_check
    repeated = ToolCall(id='check-2', name='run_command', arguments={'command': command, 'cwd': '.'})

    assert not redundant_inspections(store, verifier, task, state, executor, 'step1', [repeated])


def test_intervening_code_change_invalidates_previous_successful_check(completed_project_check):
    task, state, store, executor, verifier = completed_project_check
    store.append_event(task['task_id'], 'tool.started', {
        'tool_call_id': 'edit-again', 'tool': 'edit_file',
        'arguments': {'path': 'app.py', 'old_text': 'answer = 42', 'new_text': 'answer = 43'},
    })
    store.append_event(task['task_id'], 'tool.completed', {
        'tool_call_id': 'edit-again', 'status': 'completed', 'file_paths': ['app.py'],
    })
    repeated = ToolCall(id='check-2', name='run_command', arguments={
        'command': ['python', '-m', 'unittest', 'discover', '-s', 'tests'], 'cwd': '.',
    })

    assert not redundant_inspections(store, verifier, task, state, executor, 'step1', [repeated])


@pytest.mark.parametrize(('status', 'exit_code', 'file_paths'), [
    ('failed', 1, []),
    ('completed', 0, ['app.py']),
])
def test_failed_or_mutating_check_invalidates_earlier_success(
    completed_project_check, status, exit_code, file_paths,
):
    task, state, store, executor, verifier = completed_project_check
    store.append_event(task['task_id'], 'tool.started', {
        'tool_call_id': 'check-2', 'tool': 'run_tests',
        'arguments': {'command': ['python', '-m', 'unittest', 'discover', '-s', 'tests'], 'cwd': '.'},
    })
    store.append_event(task['task_id'], 'tool.completed' if status == 'completed' else 'tool.failed', {
        'tool_call_id': 'check-2', 'status': status, 'exit_code': exit_code, 'file_paths': file_paths,
    })
    repeated = ToolCall(id='check-3', name='run_command', arguments={
        'command': ['python', '-m', 'unittest', 'discover', '-s', 'tests'], 'cwd': '.',
    })

    assert not redundant_inspections(store, verifier, task, state, executor, 'step1', [repeated])


def test_report_only_after_redundant_check_still_runs_independent_verification(completed_project_check):
    task, state, store, _, _ = completed_project_check
    config = AppConfig.model_validate(task['config'])
    state.update({
        'next_node': 'coder',
        'last_user_event_seq': 1,
        'acceptance_command': [sys.executable, '-c', "print('independent verification')"],
        'assignment': {'step_id': 'step1', 'goal': 'Write app.py', 'requirements_revision': 1},
        'plan': {'goal': 'Write app.py', 'steps': [
            {'id': 'step1', 'goal': 'Write app.py', 'status': 'in_progress'},
        ]},
        'steps': {'step1': {'id': 'step1', 'goal': 'Write app.py', 'status': 'in_progress'}},
        'current_step_id': 'step1',
    })
    store.update_task(task['task_id'], state=state)

    class Coder:
        calls = 0

        async def complete(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(tool_calls=[ToolCall(id='repeat', name='run_command', arguments={
                    'command': ['python', '-m', 'unittest', 'discover', '-v', '-s', 'tests'],
                    'cwd': '.',
                })])
            return LLMResponse(content=(
                '{"step_id":"step1","outcome":"completed","summary":"The earlier check passed."}'
            ))

    controller = AgentController(store, object(), config)
    controller.coder.complete = Coder().complete

    async def respond(*args, **kwargs):
        return PlannerDecision(action='respond', answer='Done.')

    controller._plan = respond
    result = asyncio.run(controller.run(task['task_id']))

    events = store.read_events(task_id=task['task_id'])
    assert result['status'] == 'completed', result['state'].get('error')
    assert any(event['event_type'] == 'coder.report.only_requested' for event in events)
    verification = next(event['payload'] for event in events if event['event_type'] == 'verification.result')
    assert verification['status'] == 'passed'
    assert verification['source'] == 'explicit'
    assert 'independent verification' in verification['result']['output']
    assert any(event['event_type'] == 'tool.started'
               and event['payload'].get('tool') == 'run_tests'
               and str(event['payload'].get('tool_call_id', '')).startswith('acceptance-') for event in events)
    assert not any(event['event_type'] == 'tool.started'
                   and event['payload'].get('tool_call_id') == 'repeat' for event in events)
