from types import SimpleNamespace

import pytest

from evocode.app import Application
from evocode.config import AppConfig
from evocode.contracts import TaskSpec, ToolCall


@pytest.mark.asyncio
async def test_persisted_admission_enforces_snapshot_budget_before_tool_preparation(tmp_path):
    config = AppConfig(data_dir=str(tmp_path / 'state'))
    config.runtime.max_tool_calls = 1
    app = Application(config)
    repo = tmp_path / 'repo'
    repo.mkdir()
    task = app.create_task(TaskSpec(repo_path=str(repo), prompt='check'))
    app.store.append_event(task['task_id'], 'tool.started', {'tool_call_id': 'interrupted'})
    config.runtime.max_tool_calls = None  # A live setting cannot bypass the task snapshot.

    def forbidden_prepare(*args):
        pytest.fail('over-budget tool must not be prepared or dispatched')

    try:
        with pytest.raises(RuntimeError, match='Tool call budget exhausted'):
            await app.controller._execute(task, {}, SimpleNamespace(prepare=forbidden_prepare),
                                          ToolCall(id='next', name='read_file', arguments={'path': 'a.py'}))
        events = app.store.read_events(task_id=task['task_id'])
        assert sum(e['event_type'] == 'tool.started' for e in events) == 1
        assert events[-1]['event_type'] == 'tool.budget_exhausted'
    finally:
        await app.close()
