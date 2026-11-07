import json
from uuid import uuid4

from evocode.agent.controller import AgentController
from evocode.config import AppConfig
from evocode.contracts import TaskSpec
from evocode.persistence import EventStore
from evocode.skills import SkillRegistry


def approve(registry, version):
    evaluation_id = str(uuid4())
    payload = {'evaluation_id': evaluation_id, 'candidate_version_id': version['version_id'],
               'passed': True, 'status': 'approved'}
    with registry.store.transaction() as db:
        db.execute('INSERT INTO evaluations VALUES (?,?,?,?)',
                   (evaluation_id, None, json.dumps(payload), 'now'))
    return {'evaluation_id': evaluation_id}


def test_controller_reselects_after_plan_and_keeps_task_version_pinned(tmp_path):
    config = AppConfig(data_dir=str(tmp_path / 'data'))
    config.skills.enabled = True
    store = EventStore(config.sqlite_path)
    registry = SkillRegistry(store, tmp_path / 'data')
    try:
        task = store.create_task(TaskSpec(repo_path=str(tmp_path), repository_id='repo',
                                          prompt='Investigate a code issue'), config.snapshot())
        first = registry.create_candidate('database migration', 'pinned content', level='project',
                                          scope_id='repo', metadata={'trigger_terms': ['database migration']})
        registry.publish_version(first['version_id'], approve(registry, first))
        state = task['state']
        state['skill_snapshot'] = registry.snapshot('repo')
        controller = AgentController(store, object(), config, skills=registry)
        selected = []

        controller._refresh_skills(task, state, config, selected, 'task_start')
        assert selected == []

        state['plan'] = {'goal': 'Check the database migration behavior',
                         'steps': [{'id': 'inspect', 'goal': 'Check the database migration behavior'}]}
        controller._refresh_skills(task, state, config, selected, 'planner')
        assert [item['version_id'] for item in selected] == [first['version_id']]

        replacement = registry.create_candidate('database migration', 'new content', level='project',
                                                scope_id='repo', skill_id=first['skill_id'])
        registry.publish_version(replacement['version_id'], approve(registry, replacement))
        state['coder_messages'] = [{'role': 'tool', 'content': 'database migration failed'}]
        controller._refresh_skills(task, state, config, selected, 'execution')
        assert [item['version_id'] for item in selected] == [first['version_id']]
        assert state['loaded_skill_ids'] == [first['skill_id']]

        events = store.read_events(task_id=task['task_id'])
        selections = [event['payload'] for event in events if event['event_type'] == 'skill.selection']
        assert [item['phase'] for item in selections] == ['task_start', 'planner', 'execution']
        planner_decision = next(item for item in selections[1]['candidates']
                                if item['skill_id'] == first['skill_id'])
        assert planner_decision['reason'] == 'trigger_match'
        assert planner_decision['matches'] == [
            {'term': 'database migration', 'context': 'plan'}]
        assert selections[0]['candidates'][0]['reason'] == 'no_trigger_match'
        assert selections[2]['loaded_skill_ids'] == [first['skill_id']]
        loads = [event['payload'] for event in events if event['event_type'] == 'skill.loaded']
        assert loads == [{'skill_id': first['skill_id'], 'version_id': first['version_id'],
                          'reason': 'trigger_match',
                          'matches': [{'term': 'database migration', 'context': 'plan'}],
                          'selection_phase': 'planner'}]
    finally:
        store.close()


def test_planner_receives_selected_skills_in_context(tmp_path):
    """验证任务启动命中的技能会同步注入给 Planner 节点上下文。"""
    import asyncio
    from evocode.contracts import LLMResponse
    from evocode.context.assembler import ContextAssembler

    config = AppConfig(data_dir=str(tmp_path / 'data'))
    config.skills.enabled = True
    store = EventStore(config.sqlite_path)
    registry = SkillRegistry(store, tmp_path / 'data')
    try:
        skill = registry.create_candidate(
            'migration helper', 'SKILL_FOR_PLANNER', level='project',
            scope_id='repo', metadata={'trigger_terms': ['migration']},
        )
        registry.publish_version(skill['version_id'], approve(registry, skill))
        task = store.create_task(TaskSpec(repo_path=str(tmp_path), repository_id='repo',
                                          prompt='Fix database migration'), config.snapshot())
        state = task['state']
        state['skill_snapshot'] = registry.snapshot('repo')

        class FakePlannerLLM:
            def __init__(self):
                self.messages = []

            async def complete(self, messages, **kwargs):
                self.messages.append(messages)
                return LLMResponse(
                    content=json.dumps({'action': 'respond', 'answer': 'Handled migration.'}),
                    usage={'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
                )

        llm = FakePlannerLLM()
        controller = AgentController(store, llm, config, skills=registry)
        selected = []
        controller._refresh_skills(task, state, config, selected, 'task_start')
        assembler = ContextAssembler(store, llm, config)
        decision = asyncio.run(controller._plan(task, state, assembler, llm, skills=selected))

        assert decision.action == 'respond'
        assert 'SKILL_FOR_PLANNER' in json.dumps(llm.messages[0], ensure_ascii=False)
        events = store.read_events(task_id=task['task_id'])
        planner_ctx = next(e['payload'] for e in events
                           if e['event_type'] == 'context.assembled' and e['payload']['role'] == 'planner')
        assert planner_ctx['skill_count'] == 1
    finally:
        store.close()
