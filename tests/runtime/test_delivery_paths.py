import asyncio
import hashlib
import json
import sys

from evocode.agent import AgentController
from evocode.agent.tools import ToolExecutor
from evocode.app import Application
from evocode.config import AppConfig
from evocode.contracts import LLMResponse, SessionMessageSpec, TaskSpec, ToolCall
from evocode.persistence import EventStore


class RespondModel:
    def __init__(self, target=None):
        self.target = target

    async def complete(self, messages, *, role, **kwargs):
        assert role == 'planner'
        answer = {'action': 'respond', 'answer': 'Already done.'}
        if self.target:
            answer['requirements'] = {
                'source_user_seq': 1,
                'goal': 'Create the requested report',
                'delivery_targets': [{'path': str(self.target)}],
            }
        return LLMResponse(content=json.dumps(answer))


class MalformedReportModel:
    def __init__(self, outside, repeated_tool_calls=False):
        self.outside = outside
        self.repeated_tool_calls = repeated_tool_calls
        self.coder_calls = 0
        self.planner_calls = 0

    async def complete(self, messages, *, role, **kwargs):
        if role == 'planner':
            self.planner_calls += 1
            answer = ({'action': 'delegate', 'requirements': {'source_user_seq': 1, 'goal': 'Complete the task'}, 'assignment': {'step_id': 's1', 'goal': 'Complete the task'}}
                      if self.planner_calls == 1 else {'action': 'respond', 'answer': 'Completed.'})
            return LLMResponse(content=json.dumps(answer))
        self.coder_calls += 1
        if self.coder_calls == 1:
            return LLMResponse(content='not a report')
        if self.coder_calls == 2 or self.repeated_tool_calls and self.coder_calls == 3:
            return LLMResponse(tool_calls=[ToolCall(id='retry-edit', name='edit_file', arguments={
                'path': str(self.outside), 'old_text': '', 'new_text': 'must not run',
            })])
        return LLMResponse(content=json.dumps({
            'step_id': 's1', 'outcome': 'completed', 'summary': 'Reported completion.',
        }))


class WorkspaceCorrectionModel:
    def __init__(self, outside):
        self.outside = outside
        self.coder_calls = 0

    async def complete(self, messages, *, role, **kwargs):
        current_message = next(message for message in messages if message['content'].startswith('Current task context'))
        context = json.loads(current_message['content'].split('\n', 1)[1])
        current = context['current_requirements']
        if role == 'planner':
            if current is None:
                answer = {
                    'action': 'ask_user', 'question': '可以改到当前目录吗？',
                    'requirements': {
                        'source_user_seq': context['consumed_user_seq'],
                        'goal': 'Generate FastAPI',
                        'delivery_targets': [{'path': str(self.outside), 'kind': 'directory'}],
                    },
                }
            elif current['source_user_seq'] != context['consumed_user_seq']:
                assert context['latest_user_requirement'] == '算了，你帮我在当前工作目录新建个文件夹生成吧'
                answer = {
                    'action': 'delegate',
                    'requirements': {
                        'expected_revision': current['revision'],
                        'source_user_seq': context['consumed_user_seq'],
                        'goal': 'Generate FastAPI in a new folder in the current workspace',
                        'delivery_targets': [{'path': 'fastapi_demo/app.py'}],
                    },
                    'assignment': {
                        'step_id': 's1',
                        'goal': 'Create app.py with GET /items/{item_id}',
                        'relevant_context': [f'The old target was {self.outside}'],
                    },
                }
            else:
                answer = {'action': 'respond', 'answer': 'Generated in the current workspace.'}
            return LLMResponse(content=json.dumps(answer, ensure_ascii=False))
        assert current['revision'] == context['assignment']['requirements_revision'] == 2
        assert current['delivery_targets'][0]['path'] == 'fastapi_demo/app.py'
        self.coder_calls += 1
        if self.coder_calls == 1:
            return LLMResponse(tool_calls=[ToolCall(id='create-local', name='edit_file', arguments={
                'path': 'fastapi_demo/app.py', 'old_text': '', 'new_text': 'app = "fastapi"\n',
            })])
        return LLMResponse(content=json.dumps({
            'step_id': 's1', 'outcome': 'completed', 'summary': 'Created fastapi_demo/app.py.',
        }))


def test_absolute_sibling_target_blocks_planner_success(tmp_path):
    selected = tmp_path / 'text1 root'
    requested = tmp_path / 'text2 root'
    selected.mkdir()
    requested.mkdir()
    config = AppConfig(data_dir=str(tmp_path / 'data'))
    config.runtime.execution_backend = 'host'
    store = EventStore(config.sqlite_path)
    task = store.create_task(TaskSpec(
        repo_path=str(selected),
        prompt=f'请把报告写入“{requested / "report.md"}”，当前工作区是“{selected}”。',
    ), config.snapshot())

    result = asyncio.run(AgentController(store, RespondModel(requested / 'report.md'), config).run(task['task_id']))

    assert result['status'] == 'waiting_user'
    question = next(event['payload']['content'] for event in store.read_events(task_id=task['task_id'])
                    if event['event_type'] == 'assistant.question')
    assert '不在当前会话选择的工作区内' in question
    assert not (requested / 'report.md').exists()
    assert any(event['event_type'] == 'delivery.target_blocked'
               for event in store.read_events(task_id=task['task_id']))
    store.close()


def test_docker_execution_root_maps_only_to_selected_workspace(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    config = AppConfig(data_dir=str(tmp_path / 'data'))
    config.runtime.execution_backend = 'docker'
    store = EventStore(config.sqlite_path)
    executor = ToolExecutor(store, config)

    assert executor.path(root, '/workspace/output.txt') == root / 'output.txt'
    outside = tmp_path / 'sibling' / 'output.txt'
    try:
        executor.path(root, outside)
    except ValueError as exc:
        assert 'outside permitted workspace' in str(exc)
    else:
        raise AssertionError('host sibling path must be rejected')
    store.close()


def test_delivery_verification_requires_actual_write_evidence(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    target = root / 'result.txt'
    target.write_text('pre-existing')
    config = AppConfig(data_dir=str(tmp_path / 'data'))
    store = EventStore(config.sqlite_path)
    task = store.create_task(TaskSpec(repo_path=str(root), prompt=f'Update {target}'), config.snapshot())
    controller = AgentController(store, RespondModel(), config)

    store.append_event(task['task_id'], 'tool.started', {
        'tool_call_id': 'read', 'tool': 'read_file', 'arguments': {'path': str(target)},
    })
    store.append_event(task['task_id'], 'tool.completed', {
        'tool_call_id': 'read', 'file_paths': ['result.txt'],
    })
    state = {'current_requirements': {
        'revision': 1, 'source_user_seq': 1, 'goal': 'Update the file',
        'delivery_targets': [{'path': 'result.txt', 'operation': 'modify'}],
    }}
    assert controller.verifier.delivery_result(task, state)['status'] == 'failed'

    store.append_event(task['task_id'], 'tool.started', {
        'tool_call_id': 'edit', 'tool': 'edit_file', 'arguments': {'path': str(target)},
    })
    store.append_event(task['task_id'], 'tool.completed', {
        'tool_call_id': 'edit', 'file_paths': ['result.txt'],
    })
    assert controller.verifier.delivery_result(task, state)['status'] == 'passed'
    store.close()


def test_delivery_does_not_infer_paths_from_user_prose(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    outside = tmp_path / 'sibling' / 'old-target.txt'
    config = AppConfig(data_dir=str(tmp_path / 'data'))
    config.runtime.execution_backend = 'host'
    store = EventStore(config.sqlite_path)
    task = store.create_task(TaskSpec(
        repo_path=str(root), prompt=f'请写入“{outside}”。',
    ), config.snapshot())
    controller = AgentController(store, RespondModel(), config)

    result = controller.verifier.delivery_result(task, {'effective_prompt': '改为当前目录下的 output.txt'})

    assert result['status'] == 'not_applicable'
    store.close()


async def test_command_delivery_tracks_real_changes_without_git(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    config = AppConfig(data_dir=str(tmp_path / 'data'))
    config.runtime.execution_backend = 'host'
    store = EventStore(config.sqlite_path)
    try:
        task = store.create_task(TaskSpec(repo_path=str(root), prompt='Create a nested report'), config.snapshot())
        state = {'current_requirements': {
            'revision': 1, 'source_user_seq': 1, 'goal': 'Create a nested report',
            'delivery_targets': [
                {'path': 'notes', 'kind': 'directory', 'operation': 'create'},
                {'path': 'notes/result.txt', 'kind': 'file', 'operation': 'create'},
            ],
        }}
        controller = AgentController(store, RespondModel(), config)
        executor = ToolExecutor(store, config)

        read_only = ToolCall(id='noop', name='run_command', arguments={'command': [sys.executable, '-c', 'pass']})
        await controller._execute(task, state, executor, read_only)
        assert controller.verifier.delivery_result(task, state)['status'] == 'failed'

        write = ToolCall(id='write', name='run_command', arguments={'command': [sys.executable, '-c',
            "from pathlib import Path; Path('notes').mkdir(); Path('notes/result.txt').write_text('result')"]})
        result = await controller._execute(task, state, executor, write)
        assert result.metadata['file_paths'] == ['notes', 'notes/result.txt']
        assert controller.verifier.delivery_result(task, state)['status'] == 'passed'

        repeated = read_only.model_copy(update={'id': 'noop-again'})
        result = await controller._execute(task, state, executor, repeated)
        assert result.metadata['file_paths'] == []
    finally:
        store.close()


async def test_tool_events_capture_source_and_dependency_fingerprints(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    source = root / 'value.py'
    source.write_bytes(b'answer = 1\r\n')
    manifest = root / 'requirements.txt'
    manifest.write_bytes(b'example==1.2\n')
    config = AppConfig(data_dir=str(tmp_path / 'data'))
    store = EventStore(config.sqlite_path)
    try:
        task = store.create_task(TaskSpec(repo_path=str(root), prompt='Update the answer'), config.snapshot())
        controller = AgentController(store, RespondModel(), config)
        executor = ToolExecutor(store, config)
        read = ToolCall(id='read', name='read_file', arguments={'path': 'value.py'})
        await controller._execute(task, task['state'], executor, read)
        edit = ToolCall(id='edit', name='edit_file', arguments={
            'path': 'value.py', 'old_text': 'answer = 1', 'new_text': 'answer = 2',
        })
        await controller._execute(task, task['state'], executor, edit)
        results = [event['payload']['metadata'] for event in store.read_events(task_id=task['task_id'])
                   if event['event_type'] == 'tool.completed']
        assert results[0]['content_evidence'][0]['sha256'] == hashlib.sha256(b'answer = 1\r\n').hexdigest()
        assert results[1]['content_evidence'][0]['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert results[1]['dependency_evidence'] == [{
            'path': 'requirements.txt', 'sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
            'source': 'workspace_at_tool_execution',
        }]
    finally:
        store.close()


def test_auto_session_reply_confirms_current_workspace_target(tmp_path):
    async def scenario():
        selected = tmp_path / 'text1 root'
        requested = tmp_path / 'text2 root'
        selected.mkdir()
        requested.mkdir()
        config = AppConfig(data_dir=str(tmp_path / 'data'))
        config.runtime.execution_backend = 'host'
        config.raw_rag.enabled = False
        config.skills.enabled = False
        config.evolution.enabled = False
        app = Application(config, WorkspaceCorrectionModel(requested))
        session_id = app.create_session(str(selected))['session']['session_id']
        try:
            first = await app.add_session_message(session_id, SessionMessageSpec(
                content=f'你好帮我生成一段 FastAPI 最小实现示例代码在“{requested}”',
            ))
            task_id = first['task_id']
            blocked = await asyncio.wait_for(app.jobs[task_id], 3)
            assert blocked['status'] == 'waiting_user'

            followup = await app.add_session_message(session_id, SessionMessageSpec(
                content='算了，你帮我在当前工作目录新建个文件夹生成吧', intent='auto',
            ))
            assert followup['task_id'] == task_id
            result = await asyncio.wait_for(app.jobs[task_id], 3)

            assert result['status'] == 'completed', result
            assert (selected / 'fastapi_demo/app.py').is_file()
            assert not list(requested.iterdir())
            assert result['state']['delivery_verification']['status'] == 'passed'
            blocked_events = [event for event in app.store.read_events(task_id=task_id)
                              if event['event_type'] == 'delivery.target_blocked']
            assert len(blocked_events) == 0
            revisions = [event['payload']['requirements']['revision']
                         for event in app.store.read_events(task_id=task_id)
                         if event['event_type'] == 'requirements.updated']
            assert revisions == [1, 2]
        finally:
            await app.close()

    asyncio.run(scenario())


def test_delivery_verification_accepts_written_file_inside_requested_directory(tmp_path):
    root = tmp_path / 'workspace'
    output_dir = root / 'export folder'
    output_dir.mkdir(parents=True)
    output_file = output_dir / 'result.txt'
    output_file.write_text('result')
    config = AppConfig(data_dir=str(tmp_path / 'data'))
    config.runtime.execution_backend = 'host'
    store = EventStore(config.sqlite_path)
    task = store.create_task(TaskSpec(
        repo_path=str(root), prompt=f'你好帮我生成一段 FastAPI 最小实现示例代码在“{output_dir}”',
    ), config.snapshot())
    controller = AgentController(store, RespondModel(), config)
    store.append_event(task['task_id'], 'tool.started', {
        'tool_call_id': 'edit', 'tool': 'edit_file', 'arguments': {'path': str(output_file)},
    })
    store.append_event(task['task_id'], 'tool.completed', {
        'tool_call_id': 'edit', 'file_paths': ['export folder/result.txt'],
    })

    state = {'current_requirements': {
        'revision': 1, 'source_user_seq': 1, 'goal': 'Write files to export folder',
        'delivery_targets': [{'path': 'export folder', 'kind': 'directory'}],
    }}
    assert controller.verifier.delivery_result(task, state)['status'] == 'passed'
    store.close()


def test_malformed_report_retry_cannot_execute_tool_calls(tmp_path):
    root = tmp_path / 'workspace'
    outside = tmp_path / 'sibling' / 'must-not-exist.txt'
    root.mkdir()
    config = AppConfig(data_dir=str(tmp_path / 'data'))
    config.runtime.execution_backend = 'host'
    store = EventStore(config.sqlite_path)
    task = store.create_task(TaskSpec(repo_path=str(root), prompt='Finish the task.'), config.snapshot())

    result = asyncio.run(AgentController(store, MalformedReportModel(outside), config).run(task['task_id']))

    assert result['status'] == 'completed', result
    assert not outside.exists()
    events = store.read_events(task_id=task['task_id'])
    assert any(event['event_type'] == 'coder.report.invalid' for event in events)
    assert any(event['event_type'] == 'coder.report.retry_tool_calls_rejected' for event in events)
    store.close()


def test_repeated_tool_calls_during_report_retry_are_bounded(tmp_path):
    root = tmp_path / 'workspace'
    outside = tmp_path / 'sibling' / 'must-not-exist.txt'
    root.mkdir()
    config = AppConfig(data_dir=str(tmp_path / 'data'))
    config.runtime.execution_backend = 'host'
    store = EventStore(config.sqlite_path)
    task = store.create_task(TaskSpec(repo_path=str(root), prompt='Finish the task.'), config.snapshot())
    model = MalformedReportModel(outside, repeated_tool_calls=True)

    result = asyncio.run(AgentController(store, model, config).run(task['task_id']))

    assert result['status'] == 'failed'
    assert model.coder_calls == 3
    assert not outside.exists()
    store.close()
