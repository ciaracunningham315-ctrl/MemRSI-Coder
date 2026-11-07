import asyncio
import copy
import json
import subprocess
import uuid
from pathlib import Path

import pytest

from evocode.config import AppConfig
from evocode.contracts import LLMResponse, TaskSpec
from evocode.evolution import EvolutionService
from evocode.persistence import EventStore
from evocode.sandbox import SandboxEvaluator
from evocode.skills import SkillRegistry


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'test_check.py').write_text('def test_ok():\n    assert True\n')
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'fixture'], check=True)
    cfg = AppConfig(data_dir=str(tmp_path / 'data'))
    cfg.evolution.allow_remote_code_context = True
    store = EventStore(cfg.sqlite_path)
    registry = SkillRegistry(store, cfg.data_dir)
    task = store.create_task(TaskSpec(repo_path=str(repo), prompt='repair', acceptance_command=['python', '-m', 'pytest']), cfg.snapshot())
    store.update_task(task['task_id'], status='completed')
    yield cfg, store, registry, task, {'repo_path': str(repo), 'prompt': 'other validation issue', 'acceptance_command': ['python', '-m', 'pytest'], 'repository_id': task['repository_id']}
    store.close()


class Model:
    def __init__(self): self.calls = []
    async def complete(self, messages, **kwargs):
        data = json.loads(messages[-1]['content'])
        self.calls.append(data)
        trace = data['traces'][0]
        meta = data['kind'] == 'meta'
        strategy = data['strategy_data']
        content = 'improved learning strategy' if meta else ('improved procedure' if 'improved learning strategy' in strategy else 'initial procedure')
        return LLMResponse(content=json.dumps({'name': 'repair-skill', 'content': content,
            'operation': 'CREATE', 'level': 'meta' if meta else 'project',
            'scope_id': None if meta else trace['task']['repository_id'],
            'reason': 'verified execution evidence', 'evidence_event_ids': [trace['events'][0]['event_id']]}), usage={'total_tokens': 20})


def simulated_skill_injection_evidence(snapshot, *, context_prefix='test-context'):
    return [{
        'version_id': 'imported-' + version['version_id'],
        'content_sha256': version['content_sha256'],
        'context_id': f"{context_prefix}-{index}",
        'injection_verified': True,
        'model_call_observed': True,
    } for index, version in enumerate(snapshot.get('skill_versions', []))
      if version.get('level') == 'project' and version.get('status') == 'draft']


async def runner(spec, snapshot, workspace):
    assert Path(workspace).exists()
    assert snapshot['config']['evolution']['enabled'] is False
    assert snapshot['config']['runtime']['execution_backend'] == 'docker'
    assert snapshot['acceptance_files']['test_check.py'].startswith('def test_ok')
    assert Path(snapshot['source_data_dir'], 'evocode.db').exists()
    contents = [v['content'] for v in snapshot['skill_versions']]
    cost = 2 if 'improved procedure' in contents else (5 if contents else 10)
    return {'passed': True, 'acceptance_protected': True, 'tool_calls': cost, 'tokens': 10,
            'duration_seconds': 1,
            'coder_skill_injection_evidence': simulated_skill_injection_evidence(snapshot)}


async def test_real_candidate_lifecycle_and_publish(setup):
    cfg, store, registry, task, spec = setup
    service = EvolutionService(store, registry, Model(), cfg, runner)
    run = await service.trigger(task['task_id'])
    assert run['status'] == 'candidate'
    assert not registry.snapshot(task['repository_id'])
    run = await service.evaluate(run['run_id'], [spec])
    assert run['status'] == 'evaluated', run
    assert run['payload']['evaluation']['candidate']['tool_calls'] == 5
    published = service.publish(run['run_id'])
    assert published['status'] == 'published'
    assert registry.snapshot(task['repository_id'])
    assert service.publish(run['run_id']) == published


async def test_meta_evaluation_actually_generates_two_downstream_skills(setup):
    cfg, store, registry, task, spec = setup
    model = Model()
    service = EvolutionService(store, registry, model, cfg, runner)
    await service.trigger(task['task_id'])
    run = await service.trigger(task['task_id'], kind='meta')
    assert run['status'] == 'candidate', run
    registry_before = registry.list_versions()
    files_before = sorted(str(path) for path in registry.root.rglob('*'))
    run = await service.evaluate(run['run_id'], [spec])
    assert run['status'] == 'evaluated', run
    assert registry.list_versions() == registry_before
    assert sorted(str(path) for path in registry.root.rglob('*')) == files_before
    assert len(run['payload']['meta_stages']) == 2
    stages = run['payload']['meta_stages']
    assert [stage['branch'] for stage in stages] == ['baseline', 'candidate']
    assert stages[0]['candidate']['version_id'] != stages[1]['candidate']['version_id']
    assert all(stage['candidate']['level'] in ('project', 'general') for stage in stages)
    assert len(model.calls) == 4
    assert run['payload']['evaluation']['baseline']['tool_calls'] == 5
    assert run['payload']['evaluation']['candidate']['tool_calls'] == 2
    service.publish(run['run_id'])
    assert not registry.snapshot(task['repository_id'])  # Meta never leaks into Coder skills.


async def test_manual_meta_review_preserves_explicit_task_run_order(setup):
    cfg, store, registry, task, spec = setup
    second = store.create_task(TaskSpec(repo_path=task['repo_path'], prompt='second repair',
        repository_id=task['repository_id']), cfg.snapshot())
    store.update_task(task['task_id'], status='completed')
    store.update_task(second['task_id'], status='completed')
    model = Model()
    service = EvolutionService(store, registry, model, cfg, runner)

    first_run = await service.trigger(task['task_id'], trace_task_ids=[task['task_id']],
        idempotency_key='task-run-first')
    second_run = await service.trigger(second['task_id'], trace_task_ids=[second['task_id']],
        idempotency_key='task-run-second')
    assert first_run['status'] == second_run['status'] == 'candidate'
    # These fixture runs stand for terminal Task Gate outcomes; Meta is allowed
    # to summarize them in a caller-frozen order that differs from insertion time.
    service._save(first_run['run_id'], 'needs_review', first_run['payload'])
    service._save(second_run['run_id'], 'published', second_run['payload'])

    ordered_task_ids = [task['task_id'], second['task_id']]
    requested_run_order = [second_run['run_id'], first_run['run_id']]
    meta = await service.trigger(second['task_id'], kind='meta', trace_task_ids=ordered_task_ids,
        meta_history_run_ids=requested_run_order, idempotency_key='meta-run-ordered')
    assert meta['status'] == 'candidate', meta
    assert meta['payload']['meta_history_run_ids'] == requested_run_order
    assert meta['payload']['meta_history_ids'] == requested_run_order
    assert meta['payload']['meta_summary_run_ids'] == requested_run_order
    assert [row['run_id'] for row in model.calls[-1]['evolution_history']] == requested_run_order

    with pytest.raises(ValueError, match='different inputs'):
        await service.trigger(second['task_id'], kind='meta', trace_task_ids=ordered_task_ids,
            meta_history_run_ids=requested_run_order[::-1], idempotency_key='meta-run-ordered')


@pytest.mark.parametrize('operation', ['DEPRECATE', 'MERGE'])
async def test_meta_evaluation_applies_downstream_skill_operations_to_each_branch(setup, operation):
    cfg, store, registry, task, spec = setup
    from evocode.sandbox import SandboxEvaluator

    def publish_skill(name):
        version = registry.create_candidate(name, f'{name} instructions', level='general')
        report = {'evaluation_id': str(uuid.uuid4()), 'run_id': 'fixture',
            'candidate_version_id': version['version_id'], 'passed': True, 'status': 'approved'}
        SandboxEvaluator(store, cfg).persist(report)
        return registry.publish_version(version['version_id'], report)

    source_a, source_b = publish_skill('source-a'), publish_skill('source-b')
    seen_versions = []

    class OperationModel:
        async def complete(self, messages, **kwargs):
            data = json.loads(messages[-1]['content'])
            trace = data['traces'][0]
            if data['kind'] == 'meta':
                proposal = {'operation': 'CREATE', 'name': 'meta-strategy', 'content': 'candidate strategy',
                    'level': 'meta', 'scope_id': None, 'skill_id': None, 'parent_version_id': None,
                    'reason': 'strategy improves downstream proposals',
                    'evidence_event_ids': [trace['events'][0]['event_id']]}
            elif data['strategy_data'] == 'candidate strategy':
                proposal = {'operation': 'CREATE', 'name': 'candidate-task', 'content': 'candidate task instructions',
                    'level': 'general', 'scope_id': None,
                    'reason': 'candidate strategy produces a useful skill',
                    'evidence_event_ids': [trace['events'][0]['event_id']]}
            elif operation == 'DEPRECATE':
                proposal = {'operation': 'DEPRECATE', 'name': source_a['name'], 'content': 'retired instructions',
                    'level': 'general', 'scope_id': None, 'skill_id': source_a['skill_id'],
                    'parent_version_id': source_a['version_id'], 'reason': 'obsolete procedure',
                    'evidence_event_ids': [trace['events'][0]['event_id']]}
            else:
                proposal = {'operation': 'MERGE', 'name': 'merged-task', 'content': 'merged instructions',
                    'level': 'general', 'scope_id': None,
                    'source_skill_ids': [source_a['skill_id'], source_b['skill_id']],
                    'reason': 'combine compatible procedures',
                    'evidence_event_ids': [trace['events'][0]['event_id']]}
            return LLMResponse(content=json.dumps(proposal), usage={'total_tokens': 10})

    async def inspect_runner(spec, snapshot, workspace):
        seen_versions.append(copy.deepcopy(snapshot['skill_versions']))
        return {'passed': True, 'acceptance_protected': True,
            'tool_calls': 5 if len(seen_versions) == 1 else 2,
            'coder_skill_injection_evidence': simulated_skill_injection_evidence(
                snapshot, context_prefix=f'operation-{len(seen_versions)}')}

    service = EvolutionService(store, registry, OperationModel(), cfg, inspect_runner)
    assert (await service.trigger(task['task_id']))['status'] == 'candidate'
    meta_run = await service.trigger(task['task_id'], kind='meta')
    assert meta_run['status'] == 'candidate', meta_run
    result = await service.evaluate(meta_run['run_id'], [spec])
    assert result['status'] == 'needs_review', result
    assert 'no_comparable_meta_skill_injection_pair' in result['payload']['evaluation']['reasons']
    baseline_ids = {version['skill_id'] for version in seen_versions[0]}
    candidate_ids = {version['skill_id'] for version in seen_versions[1]}
    if operation == 'DEPRECATE':
        assert source_a['skill_id'] not in baseline_ids
        assert source_a['skill_id'] in candidate_ids
    else:
        assert source_a['skill_id'] not in baseline_ids
        assert source_b['skill_id'] not in baseline_ids
        assert source_a['skill_id'] in candidate_ids
        assert source_b['skill_id'] in candidate_ids


async def test_task_merge_publication_and_rollback(setup, monkeypatch):
    cfg, store, registry, task, spec = setup
    from types import SimpleNamespace

    import httpx

    from evocode.api import create_app
    from evocode.sandbox import SandboxEvaluator

    def publish_skill(name):
        version = registry.create_candidate(name, f'{name} instructions', level='general')
        report = {'evaluation_id': str(uuid.uuid4()), 'run_id': 'fixture',
            'candidate_version_id': version['version_id'], 'passed': True, 'status': 'approved'}
        SandboxEvaluator(store, cfg).persist(report)
        return registry.publish_version(version['version_id'], report)

    source_a, source_b = publish_skill('source-a'), publish_skill('source-b')

    class MergeModel:
        async def complete(self, messages, **kwargs):
            data = json.loads(messages[-1]['content'])
            trace = data['traces'][0]
            proposal = {'operation': 'MERGE', 'name': 'merged-skill', 'content': 'combined instructions',
                'level': 'general', 'scope_id': None,
                'source_skill_ids': [source_a['skill_id'], source_b['skill_id']],
                'reason': 'combine compatible procedures',
                'evidence_event_ids': [trace['events'][0]['event_id']]}
            return LLMResponse(content=json.dumps(proposal), usage={'total_tokens': 10})

    calls = 0

    async def inspect_runner(spec, snapshot, workspace):
        nonlocal calls
        calls += 1
        return {'passed': True, 'acceptance_protected': True,
                'tool_calls': 5 if calls == 1 else 2}

    service = EvolutionService(store, registry, MergeModel(), cfg, inspect_runner)
    run = await service.trigger(task['task_id'])
    assert run['status'] == 'candidate', run
    evaluated = await service.evaluate(run['run_id'], [spec])
    assert evaluated['status'] == 'evaluated', evaluated
    assert all(next(s for s in registry.list_skills() if s['skill_id'] == source['skill_id'])['status'] == 'active'
               for source in (source_a, source_b))

    published = service.publish(run['run_id'])
    assert published['status'] == 'published'
    statuses = {s['skill_id']: s['status'] for s in registry.list_skills()}
    assert statuses[source_a['skill_id']] == 'deprecated'
    assert statuses[source_b['skill_id']] == 'deprecated'
    assert service.publish(run['run_id']) == published

    def cannot_save(*args):
        raise RuntimeError('simulated failure writing rollback history')

    with monkeypatch.context() as patch:
        patch.setattr(service, '_save', cannot_save)
        with pytest.raises(RuntimeError, match='writing rollback history'):
            service.rollback_merge(run['run_id'])
    assert service.get_run(run['run_id']) == published
    assert registry.snapshot('repo') == {published['payload']['published_version']['skill_id']:
                                         published['payload']['published_version']['version_id']}

    api = create_app(application=SimpleNamespace(evolution=service))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://test') as client:
        response = await client.post(f"/api/evolution/{run['run_id']}/rollback")
        assert response.status_code == 200, response.text
        restored = response.json()
        retry = await client.post(f"/api/evolution/{run['run_id']}/rollback")
        assert retry.json() == restored
    assert restored['status'] == 'rolled_back'
    source_versions = {version['skill_id']: version['version_id'] for version in (source_a, source_b)}
    assert restored['payload']['rollback']['restored_source_versions'] == source_versions
    assert restored['payload']['evaluation'] == published['payload']['evaluation']
    assert restored['payload']['published_version'] == published['payload']['published_version']
    assert registry.snapshot('repo') == source_versions
    with pytest.raises(ValueError, match='passing evaluation'):
        service.publish(run['run_id'])


async def test_remote_denial_never_calls_model(setup):
    cfg, store, registry, task, spec = setup
    cfg.evolution.allow_remote_code_context = False
    model = Model()
    service = EvolutionService(store, registry, model, cfg, runner)
    run = await service.trigger(task['task_id'])
    assert run['status'] == 'skipped'
    assert model.calls == []


async def test_unprotected_acceptance_cannot_publish(setup):
    cfg, store, registry, task, spec = setup
    async def unsafe(*args): return {'passed': True, 'tool_calls': 1}
    service = EvolutionService(store, registry, Model(), cfg, unsafe)
    run = await service.trigger(task['task_id'])
    run = await service.evaluate(run['run_id'], [spec])
    assert run['status'] == 'needs_review'
    with pytest.raises(ValueError, match='trusted'): service.publish(run['run_id'])
    assert not registry.snapshot(task['repository_id'])


async def test_no_improvement_cannot_publish(setup):
    cfg, store, registry, task, spec = setup
    async def equal(*args): return {'passed': True, 'acceptance_protected': True, 'tool_calls': 5}
    service = EvolutionService(store, registry, Model(), cfg, equal)
    run = await service.trigger(task['task_id'])
    run = await service.evaluate(run['run_id'], [spec])
    assert run['status'] == 'needs_review'
    assert 'no_measured_improvement' in run['payload']['evaluation']['reasons']


async def test_unknown_evidence_rejected(setup):
    cfg, store, registry, task, spec = setup
    class BadModel(Model):
        async def complete(self, *args, **kwargs):
            response = await super().complete(*args, **kwargs)
            payload = json.loads(response.content)
            payload['evidence_event_ids'] = ['invented']
            return LLMResponse(content=json.dumps(payload))
    run = await EvolutionService(store, registry, BadModel(), cfg, runner).trigger(task['task_id'])
    assert run['status'] == 'failed'
    assert 'nonexistent' in run['payload']['error']
    assert not registry.list_skills()


async def test_runner_gets_frozen_acceptance_and_separate_data_copies(setup):
    cfg, store, registry, task, spec = setup
    seen = []

    async def isolating_runner(spec, snapshot, workspace):
        data_dir = Path(snapshot['source_data_dir'])
        marker = data_dir / 'runner-write'
        assert not marker.exists()
        marker.write_text('condition-local')
        seen.append((snapshot['acceptance_files'], data_dir))
        return {'passed': True, 'acceptance_protected': True, 'tool_calls': 2 if len(seen) == 1 else 1}

    evaluator = SandboxEvaluator(store, cfg, isolating_runner)
    frozen_acceptance = {'tests/test_frozen.py': 'assert 2 + 2 == 4\n'}
    report = await evaluator.compare('run', 'candidate', [spec],
        {'source_data_dir': cfg.data_dir, 'acceptance_files': frozen_acceptance},
        {'source_data_dir': cfg.data_dir, 'acceptance_files': frozen_acceptance})
    assert report['passed']
    assert len(seen) == 2
    assert all(mapping == frozen_acceptance for mapping, _ in seen)
    assert seen[0][1] != seen[1][1]


def _meta_evaluation_snapshots(cfg, registry, repository_id):
    meta = registry.create_candidate('meta strategy', 'meta strategy text', level='meta')
    baseline_skill = registry.create_candidate(
        'derived project skill', 'baseline derived instructions', level='project',
        scope_id=repository_id,
    )
    candidate_skill = registry.create_candidate(
        'derived project skill', 'candidate derived instructions', level='project',
        scope_id=repository_id, skill_id=baseline_skill['skill_id'],
        parent_version_id=baseline_skill['version_id'],
    )
    common = {'source_data_dir': cfg.data_dir, 'config': cfg.snapshot(),
              'acceptance_files': {'tests/test_frozen.py': 'assert True\n'}}
    baseline = {**common, 'skill_versions': [baseline_skill]}
    candidate = {**common, 'skill_versions': [candidate_skill]}
    return meta, baseline_skill, candidate_skill, baseline, candidate


async def test_meta_evaluation_scores_only_same_scope_dual_injection_rows(setup):
    cfg, store, registry, _task, spec = setup
    repository_id = spec['repository_id']
    meta, baseline_skill, candidate_skill, baseline, candidate = _meta_evaluation_snapshots(
        cfg, registry, repository_id,
    )
    observed = []

    async def runner(eval_spec, snapshot, _workspace):
        skill = snapshot['skill_versions'][0]
        same_scope = eval_spec['repository_id'] == skill['scope_id']
        observed.append((eval_spec['repository_id'], skill['content_sha256']))
        score = (3 if skill['content'] == baseline_skill['content'] else 1) if same_scope else (
            100 if skill['content'] == baseline_skill['content'] else 0
        )
        evidence = ([{
            'version_id': 'imported-' + skill['version_id'],
            'content_sha256': skill['content_sha256'],
            'context_id': 'context-' + skill['version_id'],
            'injection_verified': True,
            'model_call_observed': True,
        }] if same_scope else [])
        return {'passed': True, 'acceptance_protected': True, 'tool_calls': score,
                'coder_skill_injection_evidence': evidence}

    other_spec = {**spec, 'repository_id': 'different-repository'}
    report = await SandboxEvaluator(store, cfg, runner).compare(
        'meta-run', meta['version_id'], [spec, other_spec], baseline, candidate,
    )

    assert report['passed'] is True
    assert report['baseline']['tool_calls'] == 3
    assert report['candidate']['tool_calls'] == 1
    comparison = report['meta_comparison']
    assert comparison['comparable_pair_count'] == 1
    assert comparison['scored_pair_count'] == 1
    assert comparison['scored_row_indices'] == [0]
    assert comparison['rows'][0]['baseline_derived_skill']['source_content_sha256'] == baseline_skill['content_sha256']
    assert comparison['rows'][0]['candidate_derived_skill']['source_content_sha256'] == candidate_skill['content_sha256']
    assert comparison['rows'][0]['baseline_derived_skill']['injected_version_ids'] == [
        'imported-' + baseline_skill['version_id']
    ]
    assert comparison['rows'][0]['candidate_derived_skill']['injected_version_ids'] == [
        'imported-' + candidate_skill['version_id']
    ]
    assert comparison['rows'][1]['excluded_reason'] == 'project_scope_mismatch'


async def test_meta_evaluation_requires_hash_verified_dual_injection(setup):
    cfg, store, registry, _task, spec = setup
    meta, baseline_skill, candidate_skill, baseline, candidate = _meta_evaluation_snapshots(
        cfg, registry, spec['repository_id'],
    )

    async def runner(_spec, snapshot, _workspace):
        skill = snapshot['skill_versions'][0]
        evidence = []
        if skill['version_id'] == baseline_skill['version_id']:
            evidence = [{
                'version_id': 'imported-baseline',
                'content_sha256': skill['content_sha256'],
                'context_id': 'baseline-context',
                'injection_verified': True,
                'model_call_observed': True,
            }]
        return {'passed': True, 'acceptance_protected': True, 'tool_calls': 1,
                'coder_skill_injection_evidence': evidence}

    report = await SandboxEvaluator(store, cfg, runner).compare(
        'meta-run', meta['version_id'], [spec], baseline, candidate,
    )

    assert report['passed'] is False
    assert report['status'] == 'needs_review'
    assert 'meta_skill_injection_evidence_unverified' in report['reasons']
    assert 'no_comparable_meta_skill_injection_pair' in report['reasons']
    assert report['baseline']['tool_calls'] is None
    assert report['candidate']['tool_calls'] is None
    assert report['meta_comparison']['comparable_pair_count'] == 0
    assert report['meta_comparison']['rows'][0]['excluded_reason'] == (
        'candidate_skill_not_verified_in_coder_model_context'
    )


async def test_baseline_skills_are_pinned_when_evolution_starts(setup):
    cfg, store, registry, task, spec = setup
    from evocode.sandbox import SandboxEvaluator

    def approved_candidate(content, parent=None, skill_id=None):
        version = registry.create_candidate('baseline', content, level='general', skill_id=skill_id, parent_version_id=parent)
        report = {'evaluation_id': str(uuid.uuid4()), 'run_id': 'fixture',
            'candidate_version_id': version['version_id'], 'passed': True, 'status': 'approved'}
        SandboxEvaluator(store, cfg).persist(report)
        return registry.publish_version(version['version_id'], report, expected_parent_version_id=parent)

    first = approved_candidate('pinned v1')
    model = Model()
    service = EvolutionService(store, registry, model, cfg, runner)
    run = await service.trigger(task['task_id'])
    approved_candidate('live v2', first['version_id'], first['skill_id'])
    observed = []

    async def inspect(spec, snapshot, workspace):
        contents = [v['content'] for v in snapshot['skill_versions']]
        observed.append(contents)
        return {'passed': True, 'acceptance_protected': True,
                'tool_calls': 2 if 'initial procedure' in contents else 5}

    service.evaluator.runner = inspect
    result = await service.evaluate(run['run_id'], [spec])
    assert result['status'] == 'evaluated', result['payload'].get('evaluation')
    assert observed
    assert all('pinned v1' in versions and 'live v2' not in versions for versions in observed)


async def test_model_receives_compact_trace_and_raw_event_refs_remain_valid(setup):
    cfg, store, registry, task, spec = setup
    state = {'acceptance_command': ['python', '-m', 'pytest'],
        'plan': {'goal': 'very large plan ' + 'p' * 10000, 'steps': [{'id': 's1', 'goal': 'repair'}]},
        'coder_messages': [{'role': 'assistant', 'content': 'large message ' + 'm' * 10000}]}
    store.update_task(task['task_id'], status='completed', state=state)
    persisted_state = store.get_task(task['task_id'])['state']
    store.append_event(task['task_id'], 'tool.completed', {'tool_call_id': 'tool-1', 'status': 'completed',
        'output': 'tests passed', 'exit_code': 0, 'metadata': {'file_paths': ['src/fix.py']}})
    raw = store.read_events(task_id=task['task_id'])
    task_updated_id = next(event['event_id'] for event in reversed(raw) if event['event_type'] == 'task.updated')

    class RawReferenceModel(Model):
        async def complete(self, messages, **kwargs):
            response = await super().complete(messages, **kwargs)
            proposal = json.loads(response.content)
            proposal['evidence_event_ids'] = [task_updated_id]
            return LLMResponse(content=json.dumps(proposal))

    model = RawReferenceModel()
    run = await EvolutionService(store, registry, model, cfg, runner).trigger(task['task_id'])
    assert run['status'] == 'candidate', run
    model_trace = model.calls[0]['traces'][0]
    assert model.calls[0]['allowed_project_scope_ids'] == [task['repository_id']]
    assert set(model_trace['available_event_ids']) == {event['event_id'] for event in raw}
    assert all(event['event_type'] != 'task.updated' for event in model_trace['events'])
    assert 'plan' not in model_trace['task']['state']
    assert 'coder_messages' not in model_trace['task']['state']
    assert model_trace['task']['state']['acceptance_command'] == ['python', '-m', 'pytest']
    tool_result = next(event for event in model_trace['events'] if event['event_type'] == 'tool.completed')
    assert tool_result['payload']['output'] == 'tests passed'
    assert tool_result['payload']['metadata']['file_paths'] == ['src/fix.py']
    # Projection only builds new objects; persisted source events and state remain unchanged.
    assert store.read_events(task_id=task['task_id']) == raw
    assert store.get_task(task['task_id'])['state'] == persisted_state


async def test_oversized_model_input_is_marked_needs_review_without_llm_call(setup):
    cfg, store, registry, task, spec = setup
    cfg.models.evolution.max_context_tokens = 128
    cfg.models.evolution.max_output_tokens = 64
    model = Model()
    before = store.read_events(task_id=task['task_id'])
    run = await EvolutionService(store, registry, model, cfg, runner).trigger(task['task_id'])
    assert run['status'] == 'needs_review'
    assert run['payload']['reason'] == 'input_budget_exceeded'
    assert 'estimated_input_tokens=' in run['payload']['error']
    assert model.calls == []
    assert store.read_events(task_id=task['task_id']) == before


async def test_large_utf8_request_that_fits_token_budget_reaches_model(setup):
    cfg, store, registry, task, spec = setup
    # A large event remains available as raw evidence but is bounded in the
    # projection sent to the proposal model.
    cfg.models.evolution.max_context_tokens = 131072
    cfg.models.evolution.max_output_tokens = 4096
    store.append_event(task['task_id'], 'tool.completed', {
        'tool': 'run_tests', 'output': 'x' * 186114,
    })

    class MeasuringModel(Model):
        async def complete(self, messages, **kwargs):
            self.request_bytes = len(json.dumps(messages, ensure_ascii=False).encode('utf-8'))
            return await super().complete(messages, **kwargs)

    model = MeasuringModel()
    run = await EvolutionService(store, registry, model, cfg, runner).trigger(task['task_id'])
    assert run['status'] == 'candidate', run
    assert len(model.calls) == 1
    assert model.request_bytes < 20000
    tool_event = next(event for event in model.calls[0]['traces'][0]['events']
                      if event['event_type'] == 'tool.completed')
    assert '[truncated 186114 chars sha256=' in tool_event['payload']['output']
    assert store.read_events(task_id=task['task_id'])[-1]['payload']['output'] == 'x' * 186114


async def test_model_uses_run_config_snapshot_across_meta_proposals(setup):
    cfg, store, registry, task, spec = setup
    cfg.models.evolution.model_id = 'frozen-evolution-model'

    class ConfigurableModel:
        def __init__(self):
            self.bound_model_ids = []
            self.fake = Model()

        def for_config(self, config):
            self.bound_model_ids.append(config.models.evolution.model_id)
            return self.fake

    model = ConfigurableModel()
    service = EvolutionService(store, registry, model, cfg, runner)
    await service.trigger(task['task_id'])
    meta_run = await service.trigger(task['task_id'], kind='meta')
    assert meta_run['status'] == 'candidate'
    cfg.models.evolution.model_id = 'changed-global-model'
    result = await service.evaluate(meta_run['run_id'], [spec])
    assert result['status'] == 'evaluated', result
    assert service.llm is model
    assert model.bound_model_ids == ['frozen-evolution-model'] * 4


async def test_cancelled_trigger_persists_interrupted_and_reraises(setup):
    cfg, store, registry, task, spec = setup

    class BlockingModel:
        def __init__(self):
            self.started = asyncio.Event()

        async def complete(self, *args, **kwargs):
            self.started.set()
            await asyncio.Event().wait()

    model = BlockingModel()
    service = EvolutionService(store, registry, model, cfg, runner)
    pending = asyncio.create_task(service.trigger(task['task_id']))
    await model.started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    run = service.list_runs()[0]
    assert run['status'] == 'interrupted'
    assert run['payload']['reason'] == 'cancelled_during_proposal'
    assert run['payload']['proposal_attempts'][-1]['status'] == 'cancelled'


async def test_cancelled_evaluation_persists_interrupted_and_reraises(setup):
    cfg, store, registry, task, spec = setup
    service = EvolutionService(store, registry, Model(), cfg, runner)
    run = await service.trigger(task['task_id'])
    started = asyncio.Event()

    async def blocking_runner(*args):
        started.set()
        await asyncio.Event().wait()

    service.evaluator.runner = blocking_runner
    pending = asyncio.create_task(service.evaluate(run['run_id'], [spec]))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    interrupted = service.get_run(run['run_id'])
    assert interrupted['status'] == 'interrupted'
    assert interrupted['payload']['reason'] == 'cancelled_during_evaluation'


async def test_auto_trigger_can_be_disabled_without_disabling_manual_trigger(setup):
    cfg, store, registry, task, spec = setup
    object.__setattr__(cfg.evolution, 'auto_trigger_enabled', False)
    model = Model()
    service = EvolutionService(store, registry, model, cfg, runner)

    assert await service.on_task_finished(task['task_id']) is None
    assert model.calls == []
    assert service.list_runs() == []

    manual = await service.trigger(task['task_id'])
    assert manual['status'] == 'candidate'
    assert manual['payload']['manual'] is True
    assert len(model.calls) == 1


async def test_remote_proposal_payload_redacts_secrets_and_local_paths(setup):
    cfg, store, registry, task, spec = setup
    store.append_event(task['task_id'], 'tool.completed', {
        'api_key': 'synthetic-top-level-key',
        'result': {'password': 'synthetic-nested-password',
                   'output': f'Bearer synthetic-bearer-token and password=synthetic-inline-password; wrote {spec["repo_path"]}/src/module.py'},
        'path': '/Users/alice/private/project/file.py',
        'file_paths': ['/Users/alice/private/project/file.py', 'src/module.py'],
    })
    model = Model()
    service = EvolutionService(store, registry, model, cfg, runner)

    run = await service.trigger(task['task_id'])

    assert run['status'] == 'candidate'
    payload = model.calls[0]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert 'synthetic-' not in serialized
    assert '/Users/alice' not in serialized
    assert '[LOCAL_PATH_OMITTED]' in serialized
    assert 'src/module.py' in serialized
    assert spec['repo_path'] not in serialized
    assert '[REPOSITORY_ROOT]' in serialized
    assert 'repo_path' not in payload['traces'][0]['task']


async def test_auto_trigger_waits_for_same_repository_batch_and_groups_failed_tasks(setup):
    cfg, store, registry, task, spec = setup
    cfg.evolution.batch_task_count = 2
    cfg.evolution.similar_issue_threshold = 3
    model = Model()
    service = EvolutionService(store, registry, model, cfg, runner)

    assert await service.on_task_finished(task['task_id']) is None
    assert model.calls == []
    checked = store.read_events(task_id=task['task_id'])[-1]
    assert checked['event_type'] == 'evolution.trigger_checked'
    assert checked['payload']['reason'] == 'accumulating'

    second = store.create_task(TaskSpec(repo_path=spec['repo_path'], prompt='failed validation',
                                        acceptance_command=spec['acceptance_command']), cfg.snapshot())
    store.update_task(second['task_id'], status='failed')
    run = await service.on_task_finished(second['task_id'])

    assert run['status'] == 'candidate'
    assert model.calls
    assert set(run['payload']['trace_task_ids']) == {task['task_id'], second['task_id']}
    assert {trace['task']['repository_id'] for trace in run['payload']['traces']} == {task['repository_id']}


async def test_autonomous_lifecycle_evaluates_selection_specs_and_publishes_only_after_gates(setup):
    cfg, store, registry, task, source_spec = setup
    cfg.evolution.batch_task_count = 1
    cfg.evolution.auto_evaluate = True
    cfg.evolution.auto_publish = True
    selection_spec = {**source_spec, 'prompt': 'held-out selection prompt'}
    cfg.evolution.selection_task_specs = [selection_spec]
    assert 'selection_task_specs' not in cfg.snapshot()['evolution']
    seen_specs = []
    seen_configs = []

    async def selection_runner(spec, snapshot, workspace):
        seen_specs.append(spec['prompt'])
        seen_configs.append(snapshot['config'])
        return await runner(spec, snapshot, workspace)

    model = Model()
    service = EvolutionService(store, registry, model, cfg, selection_runner)

    run = await service.on_task_finished(task['task_id'])

    assert run['status'] == 'published', run
    assert 'selection_task_specs' not in run['payload']['config']['evolution']
    assert run['payload']['evaluation']['passed'] is True
    assert [row['spec']['prompt'] for row in run['payload']['evaluation']['rows']] == ['held-out selection prompt']
    row = run['payload']['evaluation']['rows'][0]
    pinned_commit = subprocess.check_output(
        ['git', '-C', source_spec['repo_path'], 'rev-parse', 'HEAD'], text=True,
    ).strip()
    assert row['spec']['commit'] == pinned_commit
    assert row['baseline']['commit'] == row['candidate']['commit'] == pinned_commit
    assert seen_specs == ['held-out selection prompt', 'held-out selection prompt']
    assert seen_configs[0] == seen_configs[1]
    assert registry.snapshot(task['repository_id'])
    assert run['payload']['trace_task_ids'] == [task['task_id']]


async def test_automatic_meta_review_uses_the_same_evaluate_and_publish_gates(setup):
    cfg, store, registry, task, spec = setup
    cfg.evolution.batch_task_count = 1
    cfg.evolution.meta_review_after_runs = 1
    cfg.evolution.auto_evaluate = True
    cfg.evolution.auto_publish = True
    cfg.evolution.selection_task_specs = [spec]
    model = Model()
    service = EvolutionService(store, registry, model, cfg, runner)

    task_run = await service.on_task_finished(task['task_id'])
    meta_runs = [run for run in service.list_runs() if run['kind'] == 'meta']

    assert task_run['status'] == 'published', task_run
    assert len(meta_runs) == 1
    assert meta_runs[0]['status'] == 'published', meta_runs[0]
    assert meta_runs[0]['payload']['evaluation']['passed'] is True
    assert len(meta_runs[0]['payload']['evaluation']['meta_stages']) == 2
    published = meta_runs[0]['payload']['published_version']
    assert published['level'] == 'meta'
    assert service.active_meta()['version_id'] == published['version_id']
    assert service.active_meta()['source'] == 'registry'
    assert task_run['meta_skill_version_id'] is None

    later_task = store.create_task(
        TaskSpec(repo_path=spec['repo_path'], prompt='repair a different issue'),
        cfg.snapshot(),
    )
    store.update_task(later_task['task_id'], status='completed')
    later_run = await service.on_task_finished(later_task['task_id'])
    assert later_run['meta_skill_version_id'] == published['version_id']
    assert later_run['payload']['base_meta']['content'] == 'improved learning strategy'
    assert later_run['payload']['candidate']['content'] == 'improved procedure'


async def test_private_oracle_path_is_loaded_for_eval_but_redacted_from_reports(setup, tmp_path):
    cfg, store, registry, task, spec = setup
    oracle_path = tmp_path / 'private' / 'oracle.py'
    oracle_path.parent.mkdir()
    oracle_path.write_text('SECRET_ORACLE_ASSERTION = True\n')
    private_spec = {
        **spec,
        'acceptance_files': ['tests/private_acceptance.py'],
        'protected_acceptance_paths': {'tests/private_acceptance.py': str(oracle_path)},
    }
    observed = []

    async def protected_runner(eval_spec, snapshot, workspace):
        assert snapshot['oracle_only_acceptance'] is True
        assert snapshot['acceptance_files'] == {
            'tests/private_acceptance.py': 'SECRET_ORACLE_ASSERTION = True\n',
        }
        assert not (Path(workspace) / 'tests' / 'private_acceptance.py').exists()
        observed.append(True)
        return {
            'passed': True,
            'acceptance_protected': True,
            'tool_calls': 1 if snapshot['skill_versions'] else 3,
        }

    model = Model()
    service = EvolutionService(store, registry, model, cfg, protected_runner)
    candidate = await service.trigger(task['task_id'])
    evaluated = await service.evaluate(candidate['run_id'], [private_spec])

    assert evaluated['status'] == 'evaluated', evaluated
    assert observed == [True, True]
    report_spec = evaluated['payload']['evaluation']['rows'][0]['spec']
    assert 'protected_acceptance_paths' not in report_spec
    assert 'SECRET_ORACLE_ASSERTION' not in json.dumps(evaluated['payload'])
    assert 'protected_acceptance_paths' not in json.dumps(model.calls)


async def test_oracle_path_inside_source_repository_is_rejected(setup):
    cfg, store, registry, task, spec = setup
    called = []

    async def protected_runner(*args):
        called.append(True)
        return {'passed': True, 'acceptance_protected': True, 'tool_calls': 1}

    spec = {
        **spec,
        'acceptance_files': ['tests/private_acceptance.py'],
        'protected_acceptance_paths': {
            'tests/private_acceptance.py': str(Path(spec['repo_path']) / 'test_check.py'),
        },
    }
    result = await SandboxEvaluator(store, cfg, protected_runner)._execute(spec, {
        'source_data_dir': cfg.data_dir,
        'skill_versions': [],
        'config': cfg.snapshot(),
    })

    assert result['unverified'] is True
    assert 'outside the source' in result['error']
    assert called == []


async def test_eval_database_snapshot_includes_uncheckpointed_wal_events(setup, tmp_path):
    cfg, store, registry, task, spec = setup
    marker = 'committed-only-in-wal'
    store.append_event(task['task_id'], 'test.result', {'wal_marker': marker})
    db_path = Path(cfg.data_dir) / 'evocode.db'
    assert Path(str(db_path) + '-wal').exists()
    import shutil
    main_file_copy = tmp_path / 'main-file-only.db'
    shutil.copy2(db_path, main_file_copy)
    import sqlite3
    main_only = sqlite3.connect(main_file_copy)
    try:
        has_events = main_only.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
        main_rows = (main_only.execute(
            "SELECT payload_json FROM events WHERE task_id=? AND event_type='test.result'",
            (task['task_id'],),
        ).fetchall() if has_events else [])
    finally:
        main_only.close()
    assert not any(json.loads(row[0]).get('wal_marker') == marker for row in main_rows)
    oracle_path = tmp_path / 'oracle' / 'acceptance.py'
    oracle_path.parent.mkdir()
    oracle_path.write_text('ASSERTION = True\n')

    async def snapshot_runner(eval_spec, snapshot, workspace):
        import sqlite3

        conn = sqlite3.connect(Path(snapshot['source_data_dir']) / 'evocode.db')
        try:
            rows = conn.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type='test.result'",
                (task['task_id'],),
            ).fetchall()
        finally:
            conn.close()
        assert any(json.loads(row[0]).get('wal_marker') == marker for row in rows)
        assert snapshot['oracle_only_acceptance'] is True
        return {'passed': True, 'acceptance_protected': True, 'tool_calls': 1}

    spec = {
        **spec,
        'protected_acceptance_paths': {'tests/private_acceptance.py': str(oracle_path)},
    }
    result = await SandboxEvaluator(store, cfg, snapshot_runner)._execute(spec, {
        'source_data_dir': cfg.data_dir,
        'skill_versions': [],
        'config': cfg.snapshot(),
    })

    assert result['passed'] is True, result


async def test_auto_evaluate_does_not_bypass_trigger_thresholds(setup):
    cfg, store, registry, task, spec = setup
    cfg.evolution.batch_task_count = 2
    cfg.evolution.similar_issue_threshold = 3
    cfg.evolution.auto_evaluate = True
    cfg.evolution.auto_publish = True
    cfg.evolution.selection_task_specs = [spec]
    model = Model()
    service = EvolutionService(store, registry, model, cfg, runner)

    assert await service.on_task_finished(task['task_id']) is None
    assert model.calls == []
    assert service.list_runs() == []
    assert not registry.snapshot(task['repository_id'])


async def test_freezing_meta_skill_skips_meta_proposal_but_keeps_task_evolution(setup):
    cfg, store, registry, task, spec = setup
    cfg.evolution.freeze_meta_skill = True
    model = Model()
    service = EvolutionService(store, registry, model, cfg, runner)

    meta = await service.trigger(task['task_id'], kind='meta')
    task_run = await service.trigger(task['task_id'], kind='task')

    assert meta['status'] == 'skipped'
    assert meta['payload']['reason'] == 'meta_skill_frozen'
    assert task_run['status'] == 'candidate'
    assert len(model.calls) == 1


async def test_repeated_issue_triggers_group_and_duplicate_auto_callback_is_idempotent(setup):
    cfg, store, registry, task, spec = setup
    cfg.evolution.batch_task_count = 8
    cfg.evolution.similar_issue_threshold = 2
    store.append_event(task['task_id'], 'tool.failed', {'error': 'Package resolver failed with marker-123'})
    second = store.create_task(TaskSpec(repo_path=spec['repo_path'], prompt='another resolver failure',
                                        acceptance_command=spec['acceptance_command']), cfg.snapshot())
    store.update_task(second['task_id'], status='failed')
    store.append_event(second['task_id'], 'tool.failed', {'error': 'Package resolver failed with marker-123'})
    model = Model()
    service = EvolutionService(store, registry, model, cfg, runner)

    run = await service.on_task_finished(second['task_id'])
    again = await service.on_task_finished(second['task_id'])

    assert run['status'] == 'candidate'
    assert again['run_id'] == run['run_id']
    assert len(model.calls) == 1
    assert set(run['payload']['trace_task_ids']) == {task['task_id'], second['task_id']}


async def test_meta_review_is_reserved_once_per_feedback_milestone(setup):
    cfg, store, registry, task, spec = setup
    cfg.evolution.meta_review_after_runs = 1
    model = Model()
    service = EvolutionService(store, registry, model, cfg, runner)
    evaluated = await service.trigger(task['task_id'])
    assert (await service.evaluate(evaluated['run_id'], [spec]))['status'] == 'evaluated'
    assert (await service.trigger(task['task_id']))['status'] == 'candidate'

    await service.on_task_finished(task['task_id'])
    await service.on_task_finished(task['task_id'])

    meta_runs = [run for run in service.list_runs() if run['kind'] == 'meta']
    assert len(meta_runs) == 1
    assert meta_runs[0]['payload']['meta_review_count'] == 1
    assert len(meta_runs[0]['payload']['meta_history_ids']) == 2
    assert sum(call['kind'] == 'meta' for call in model.calls) == 1


async def test_proposal_normalizes_trigger_terms_and_recovers_on_retry(setup):
    """验证演化提案支持触发词归一化与单次校验失败后的重试修复。"""
    cfg, store, registry, task, spec = setup

    class FlakyThenNormalizedModel:
        def __init__(self):
            self.calls = []

        async def complete(self, messages, **kwargs):
            data = json.loads(messages[-1]['content'])
            self.calls.append(data)
            trace = data['traces'][0]
            if len(self.calls) == 1:
                return LLMResponse(content='{"name": "", "content": "incomplete"', usage={'total_tokens': 15})
            return LLMResponse(
                content=json.dumps({
                    'name': 'normalized-skill',
                    'content': 'complete instructions',
                    'operation': 'REVISE',
                    'skill_id': None,
                    'level': 'project',
                    'scope_id': 'wrong-scope-id',
                    'trigger_terms': 'api_compat, legacy_key, a, ' + ', '.join(f'term_{i}' for i in range(10)),
                    'reason': 'repaired on second attempt',
                    'evidence_event_ids': [trace['events'][0]['event_id']],
                }),
                usage={'total_tokens': 25},
            )

    model = FlakyThenNormalizedModel()
    service = EvolutionService(store, registry, model, cfg, runner)
    run = await service.trigger(task['task_id'])
    assert run['status'] == 'candidate', run
    assert len(model.calls) == 2
    assert 'validation_repair' in model.calls[1]
    proposal = run['payload']['proposal']
    assert proposal['operation'] == 'CREATE'
    assert proposal['scope_id'] == task['repository_id']
    assert len(proposal['trigger_terms']) == 8
    assert 'a' not in proposal['trigger_terms']
    assert run['payload']['usage']['total_tokens'] == 40


async def test_length_truncation_repairs_with_same_output_cap_and_records_attempts(setup):
    cfg, store, registry, task, spec = setup

    class LengthThenValidModel:
        def __init__(self):
            self.requests = []

        async def complete(self, messages, **kwargs):
            self.requests.append((json.loads(messages[-1]['content']), kwargs))
            if len(self.requests) == 1:
                return LLMResponse(content='{"name":"partial', finish_reason='length',
                                   response_id='resp-length', usage={'total_tokens': 7})
            data = self.requests[-1][0]
            trace = data['traces'][0]
            return LLMResponse(content=json.dumps({'name': 'repaired', 'content': 'short procedure',
                'reason': 'based on trace evidence', 'evidence_event_ids': [trace['events'][0]['event_id']]}),
                finish_reason='stop', response_id='resp-valid', usage={'total_tokens': 11})

    model = LengthThenValidModel()
    run = await EvolutionService(store, registry, model, cfg, runner).trigger(task['task_id'])

    assert run['status'] == 'candidate', run
    assert len(model.requests) == 2
    attempts = run['payload']['proposal_attempts']
    assert [item['status'] for item in attempts] == ['invalid', 'valid']
    assert attempts[0]['error_type'] == 'length_truncation'
    assert attempts[0]['finish_reason'] == 'length'
    assert attempts[0]['response_id'] == 'resp-length'
    assert attempts[1]['representation'] == 'legacy_content'
    assert model.requests[1][0]['validation_repair']['error'] == attempts[0]['error']
    assert model.requests[1][0]['validation_repair']['previous_bad_output'] == '{"name":"partial'
    assert [item[1]['max_tokens'] for item in model.requests] == [cfg.models.evolution.max_output_tokens] * 2
    assert run['payload']['usage'] == {'total_tokens': 18}
    assert all(item['usage_complete'] for item in attempts)
    assert all(item['raw_output_artifact_id'] for item in attempts)


async def test_failed_retry_keeps_prior_usage_and_raw_output_artifact(setup):
    cfg, store, registry, task, spec = setup
    from evocode.llm import ModelError

    class InvalidThenTransportFailure:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(content='not json', usage={'total_tokens': 13}, finish_reason='stop')
            raise ModelError('request failed after retry', usage={'total_tokens': 5})

    model = InvalidThenTransportFailure()
    run = await EvolutionService(store, registry, model, cfg, runner).trigger(task['task_id'])

    assert run['status'] == 'failed'
    attempts = run['payload']['proposal_attempts']
    assert [item['status'] for item in attempts] == ['invalid', 'transport_error']
    assert [item['error_type'] for item in attempts] == ['json', 'transport']
    assert run['payload']['usage'] == {'total_tokens': 18}
    assert attempts[0]['raw_output_artifact_id']
    artifact = store.get_artifact(attempts[0]['raw_output_artifact_id'])
    assert Path(artifact['path']).read_text() == 'not json'
    assert attempts[0]['usage_complete'] is True
    assert attempts[1]['usage_complete'] is True


@pytest.mark.parametrize('invalid_output,category', [
    ('   ', 'whitespace'),
    ('{"body":', 'json'),
    ('{"name":42}', 'schema'),
])
async def test_invalid_proposal_repair_is_bounded_and_keeps_both_bills(setup, invalid_output, category):
    cfg, store, registry, task, spec = setup

    class InvalidModel:
        def __init__(self):
            self.requests = []

        async def complete(self, messages, **kwargs):
            self.requests.append(json.loads(messages[-1]['content']))
            return LLMResponse(content=invalid_output, raw_message={'content': invalid_output},
                               usage={'total_tokens': 9}, finish_reason='stop')

    model = InvalidModel()
    result = await EvolutionService(store, registry, model, cfg, runner).trigger(task['task_id'])
    assert result['status'] == 'failed'
    assert result['payload']['usage']['total_tokens'] == 18
    assert len(model.requests) == 2
    assert model.requests[1]['validation_repair']['previous_bad_output'] == invalid_output
    assert all(a['error_type'] == category for a in result['payload']['proposal_attempts'])
    assert not registry.snapshot(task['repository_id'])


@pytest.mark.parametrize('format_name', ['json_object', 'json_schema', 'strict_tool'])
async def test_structured_proposals_render_markdown_through_each_explicit_transport(setup, format_name):
    from evocode.contracts import ToolCall

    cfg, store, registry, task, spec = setup
    cfg.evolution.proposal_format = format_name
    requests = []

    class StructuredModel:
        async def complete(self, messages, **kwargs):
            requests.append(kwargs)
            data = json.loads(messages[-1]['content'])
            value = {
                'name': 'structured repair', 'operation': 'CREATE', 'level': 'project',
                'scope_id': task['repository_id'], 'reason': 'Observed repair',
                'evidence_event_ids': [data['allowed_evidence_event_ids'][0]],
                'body': {'applicability': 'Repair this module',
                         'steps': ['Inspect output', 'Run tests'], 'limitations': []},
            }
            if format_name == 'strict_tool':
                assert kwargs['tools'][0]['function']['strict'] is True
                return LLMResponse(tool_calls=[ToolCall(id='proposal-1', name='submit_skill_proposal',
                                                       arguments=value)],
                                   finish_reason='tool_calls', usage={'total_tokens': 12})
            assert kwargs['response_format']['type'] == format_name
            return LLMResponse(content=json.dumps(value), usage={'total_tokens': 12}, finish_reason='stop')

    result = await EvolutionService(store, registry, StructuredModel(), cfg, runner).trigger(task['task_id'])
    assert result['status'] == 'candidate', result
    assert len(requests) == 1
    assert '## Steps' in result['payload']['candidate']['content']
    attempt = result['payload']['proposal_attempts'][0]
    assert attempt['representation'] == 'structured_body'
    assert attempt['usage_complete'] is True
    artifact = store.get_artifact(attempt['raw_output_artifact_id'])
    assert Path(artifact['path']).read_text()
    assert not registry.snapshot(task['repository_id'])


async def test_meta_downstream_failure_preserves_usage_without_double_counting(setup):
    cfg, store, registry, task, spec = setup

    class FailingStageModel(Model):
        async def complete(self, messages, **kwargs):
            if len(self.calls) >= 2:
                self.calls.append(json.loads(messages[-1]['content']))
                return LLMResponse(content='bad proposal', finish_reason='stop', usage={'total_tokens': 5})
            return await super().complete(messages, **kwargs)

    model = FailingStageModel()
    service = EvolutionService(store, registry, model, cfg, runner)
    await service.trigger(task['task_id'])
    meta = await service.trigger(task['task_id'], kind='meta')
    assert meta['status'] == 'candidate'
    result = await service.evaluate(meta['run_id'], [spec])
    assert result['status'] == 'failed'
    assert result['payload']['usage']['total_tokens'] == 20
    assert result['payload']['meta_stages'][0]['usage']['total_tokens'] == 10
    assert len(result['payload']['meta_stages'][0]['proposal_attempts']) == 2
    assert not registry.snapshot(task['repository_id'])


async def test_cancelled_repair_keeps_paid_first_attempt(setup):
    cfg, store, registry, task, spec = setup

    class CancelledRepairModel:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(content='bad JSON', usage={'total_tokens': 7})
            raise asyncio.CancelledError

    service = EvolutionService(store, registry, CancelledRepairModel(), cfg, runner)
    with pytest.raises(asyncio.CancelledError):
        await service.trigger(task['task_id'])
    result = service.list_runs()[0]
    assert result['status'] == 'interrupted'
    assert result['payload']['usage']['total_tokens'] == 7
    assert result['payload']['proposal_attempts'][-1]['usage_complete'] is False


def test_state_delta_projection_omits_duplicate_context_and_keeps_raw_evidence(setup):
    cfg, store, registry, task, spec = setup
    duplicate = [{'role': 'user', 'content': 'large repeated context ' * 4000}]
    event_id = store.append_event(task['task_id'], 'task.state.delta', {
        'set': {'planner_messages': duplicate, 'coder_messages': duplicate,
                'next_node': 'planner', 'error': 'test failure'},
        'remove': [], 'status': 'running',
    })['event_id']
    service = EvolutionService(store, registry, Model(), cfg, runner)
    raw = service._trace(task['task_id'])
    projected = service._project_traces(raw)
    assert len(json.dumps(projected)) < 12000
    delta = next(e for e in projected[0]['events'] if e['event_id'] == event_id)
    assert delta['payload']['execution']['next_node'] == 'planner'
    assert 'planner_messages' in delta['payload']['changed_fields']
    persisted = next(e for e in store.read_events(task_id=task['task_id']) if e['event_id'] == event_id)
    assert persisted['payload']['set']['planner_messages'] == duplicate


async def test_merge_candidate_that_targets_a_source_is_repaired_before_creation(setup):
    cfg, store, registry, task, spec = setup
    from evocode.sandbox import SandboxEvaluator

    def publish_skill(name):
        version = registry.create_candidate(name, f'{name} instructions', level='general')
        report = {'evaluation_id': str(uuid.uuid4()), 'run_id': 'fixture',
            'candidate_version_id': version['version_id'], 'passed': True, 'status': 'approved'}
        SandboxEvaluator(store, cfg).persist(report)
        return registry.publish_version(version['version_id'], report)

    source_a, source_b = publish_skill('merge-source-a'), publish_skill('merge-source-b')

    class InvalidMergeThenValidModel:
        def __init__(self):
            self.requests = []

        async def complete(self, messages, **kwargs):
            data = json.loads(messages[-1]['content'])
            self.requests.append(data)
            trace = data['traces'][0]
            proposal = {'operation': 'MERGE', 'name': 'combined skill', 'content': 'combined instructions',
                'level': 'general', 'scope_id': None, 'skill_id': source_a['skill_id'],
                'parent_version_id': source_a['version_id'],
                'source_skill_ids': [source_a['skill_id'], source_b['skill_id']],
                'reason': 'combine two compatible procedures',
                'evidence_event_ids': [trace['events'][0]['event_id']]}
            if 'validation_repair' in data:
                proposal['skill_id'] = None
                proposal['parent_version_id'] = None
            return LLMResponse(content=json.dumps(proposal), usage={'total_tokens': 10})

    model = InvalidMergeThenValidModel()
    service = EvolutionService(store, registry, model, cfg, runner)
    run = await service.trigger(task['task_id'])

    assert run['status'] == 'candidate', run
    assert len(model.requests) == 2
    assert model.requests[1]['validation_repair']['error_type'] == 'merge_target'
    assert 'set skill_id and parent_version_id to null' in model.requests[1]['validation_repair']['instruction'].lower()
    assert run['payload']['proposal']['skill_id'] is None
    assert run['payload']['proposal']['source_skill_ids'] == [source_a['skill_id'], source_b['skill_id']]
    assert [attempt['status'] for attempt in run['payload']['proposal_attempts']] == ['invalid', 'valid']
    assert run['payload']['proposal_attempts'][0]['error_type'] == 'merge_target'


async def test_revise_retains_parent_triggers_and_meta_history_includes_proposal_details(setup):
    """验证 REVISE 提案保留父版本高召回触发词，且 Meta 提案历史包含触发词与正文摘要。"""
    cfg, store, registry, task, spec = setup
    parent = registry.create_candidate(
        'api-skill', 'v1 instructions', level='project', scope_id=task['repository_id'],
        metadata={'trigger_terms': ['api_material.py', 'keyword-only']},
    )
    report = {'evaluation_id': str(uuid.uuid4()), 'run_id': 'fixture',
              'candidate_version_id': parent['version_id'], 'passed': True, 'status': 'approved'}
    SandboxEvaluator(store, cfg).persist(report)
    registry.publish_version(parent['version_id'], report)

    class ReviseAndMetaModel:
        def __init__(self):
            self.calls = []

        async def complete(self, messages, **kwargs):
            data = json.loads(messages[-1]['content'])
            self.calls.append(data)
            trace = data['traces'][0]
            if data['kind'] == 'task':
                return LLMResponse(content=json.dumps({
                    'name': 'api-skill', 'operation': 'REVISE', 'level': 'project',
                    'scope_id': task['repository_id'], 'skill_id': parent['skill_id'],
                    'parent_version_id': parent['version_id'],
                    'trigger_terms': ['headers Mapping'],
                    'content': 'v2 instructions', 'reason': 'extend for headers',
                    'evidence_event_ids': [trace['events'][0]['event_id']],
                }), usage={'total_tokens': 12})
            return LLMResponse(content=json.dumps({
                'name': 'meta-v2', 'operation': 'CREATE', 'level': 'meta',
                'scope_id': None, 'content': 'improved meta strategy',
                'reason': 'preserve trigger recall', 'evidence_event_ids': [],
            }), usage={'total_tokens': 12})

    model = ReviseAndMetaModel()
    service = EvolutionService(store, registry, model, cfg, runner)
    task_run = await service.trigger(task['task_id'], kind='task')
    assert task_run['status'] == 'candidate'
    assert task_run['payload']['proposal']['trigger_terms'] == [
        'headers Mapping', 'api_material.py', 'keyword-only'
    ]

    meta_run = await service.trigger(task['task_id'], kind='meta')
    assert meta_run['status'] == 'candidate'
    history_item = model.calls[-1]['evolution_history'][0]['proposal']
    assert history_item['trigger_terms'] == ['headers Mapping', 'api_material.py', 'keyword-only']
    assert history_item['content'] == 'v2 instructions'


async def test_project_scoped_task_skill_evaluation_ignores_out_of_scope_repository_noise(setup):
    """验证项目级 Task Skill 评估只按所属仓库计分，不受无关仓库的随机波动干扰。"""
    cfg, store, registry, task, spec = setup
    other_spec = {**spec, 'repository_id': 'other-unrelated-repo'}

    async def noisy_multi_repo_runner(eval_spec, snapshot, workspace):
        has_candidate = bool(snapshot.get('skill_versions'))
        if eval_spec['repository_id'] == task['repository_id']:
            cost = 4 if has_candidate else 6
        else:
            # 无关仓库在候选分支上模拟随机变慢，不应导致目标仓库有效候选被拒
            cost = 12 if has_candidate else 5
        return {'passed': True, 'acceptance_protected': True, 'tool_calls': cost, 'tokens': 10}

    service = EvolutionService(store, registry, Model(), cfg, noisy_multi_repo_runner)
    run = await service.trigger(task['task_id'])
    evaluated = await service.evaluate(run['run_id'], [spec, other_spec])
    assert evaluated['status'] == 'evaluated', evaluated['payload']['evaluation']
    assert evaluated['payload']['evaluation']['passed'] is True
    assert evaluated['payload']['evaluation']['baseline']['tool_calls'] == 6
    assert evaluated['payload']['evaluation']['candidate']['tool_calls'] == 4


async def test_multi_repository_meta_evaluation_and_regression_task_filtering(setup, tmp_path):
    """验证跨多仓库 Meta 评估按每个仓库分别派生下游 Skill，且回归重放题不稀释成本改进也不导致 Meta 证据不完整。"""
    cfg, store, registry, task, spec = setup
    repo_b_dir = tmp_path / 'repo_b'
    repo_b_dir.mkdir()
    (repo_b_dir / 'app.py').write_text('x = 1\n')
    (repo_b_dir / 'test_check.py').write_text('def test_ok():\n    assert True\n')
    subprocess.run(['git', 'init'], cwd=repo_b_dir, check=True, capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 't@e.com'], cwd=repo_b_dir, check=True)
    subprocess.run(['git', 'config', 'user.name', 't'], cwd=repo_b_dir, check=True)
    subprocess.run(['git', 'add', '.'], cwd=repo_b_dir, check=True)
    subprocess.run(['git', 'commit', '-m', 'init'], cwd=repo_b_dir, check=True, capture_output=True)

    task_b = store.create_task(TaskSpec(repo_path=str(repo_b_dir), prompt='fix repo b',
                                        acceptance_command=spec['acceptance_command']), cfg.snapshot())
    store.update_task(task_b['task_id'], status='completed')
    store.append_event(task_b['task_id'], 'test.result', {'passed': True})

    spec_b = {
        'task_id': 'repo_b_gate_meta',
        'repository_id': task_b['repository_id'],
        'repo_path': str(repo_b_dir),
        'prompt': 'evaluate repo b meta gate',
        'acceptance_command': spec['acceptance_command'],
        'acceptance_files': ['test_check.py'],
        'gate_kind': 'meta',
    }
    replay_spec = {
        **spec,
        'task_id': 'repo_a_replay_status',
        'gate_kind': 'regression',
    }

    async def multi_repo_meta_runner(eval_spec, snapshot, workspace):
        versions = [
            v for v in snapshot.get('skill_versions', [])
            if v.get('level') == 'project' and v.get('scope_id') == eval_spec['repository_id']
        ]
        if eval_spec.get('gate_kind') == 'regression':
            # 回归题在基线与候选上成本一致且均通过
            return {'passed': True, 'acceptance_protected': True, 'tool_calls': 2, 'tokens': 10}
        assert len(versions) == 1
        assert versions[0].get('metadata', {}).get('always_load') is True
        skill = versions[0]
        is_candidate = 'candidate-meta-guidance' in str(skill.get('content') or '')
        return {
            'passed': True,
            'acceptance_protected': True,
            'tool_calls': 2 if is_candidate else 5,
            'tokens': 10,
            'coder_skill_injection_evidence': simulated_skill_injection_evidence(snapshot),
        }

    class MultiRepoMetaModel:
        def __init__(self):
            self.calls = []

        async def complete(self, messages, **kwargs):
            data = json.loads(messages[-1]['content'])
            self.calls.append(data)
            if data['kind'] == 'meta':
                return LLMResponse(content=json.dumps({
                    'name': 'meta-v2', 'operation': 'CREATE', 'level': 'meta',
                    'scope_id': None, 'content': 'candidate-meta-guidance',
                    'reason': 'multi-repo meta review',
                    'evidence_event_ids': [data['allowed_evidence_event_ids'][0]],
                }), usage={'total_tokens': 10})
            trace = data['traces'][0]
            repo_id = trace['task']['repository_id']
            strategy_text = str(data.get('strategy_data') or '')
            tag = 'candidate-meta-guidance' if 'candidate-meta-guidance' in strategy_text else 'baseline-meta'
            return LLMResponse(content=json.dumps({
                'name': f"derived-{repo_id}",
                'operation': 'CREATE',
                'level': 'project',
                'scope_id': repo_id,
                'content': f"{tag} for {repo_id}",
                'reason': 'derived skill',
                'evidence_event_ids': [trace['events'][0]['event_id']],
            }), usage={'total_tokens': 10})

    service = EvolutionService(store, registry, MultiRepoMetaModel(), cfg, multi_repo_meta_runner)
    prior_task_run = await service.trigger(task['task_id'], kind='task')
    assert prior_task_run['status'] == 'candidate'
    meta_run = await service.trigger(trace_task_ids=[task['task_id'], task_b['task_id']], kind='meta')
    assert meta_run['status'] == 'candidate'

    evaluated = await service.evaluate(meta_run['run_id'], [
        {**spec, 'task_id': 'repo_a_gate_meta', 'gate_kind': 'meta'},
        spec_b,
        replay_spec,
    ])
    assert evaluated['status'] == 'evaluated', evaluated['payload']['evaluation']
    report = evaluated['payload']['evaluation']
    assert report['passed'] is True, report
    assert report['meta_comparison']['evidence_complete'] is True
    assert report['meta_comparison']['scored_pair_count'] == 2
    assert report['baseline']['tool_calls'] == 10
    assert report['candidate']['tool_calls'] == 4

