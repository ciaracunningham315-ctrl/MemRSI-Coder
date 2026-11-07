import json
from uuid import uuid4

import pytest

from evocode.persistence import EventStore
from evocode.skills import SkillRegistry


@pytest.fixture
def registry(tmp_path):
    store = EventStore(tmp_path / 'db.sqlite')
    yield SkillRegistry(store, tmp_path)
    store.close()


def approve(registry, version, passed=True, promotion=None):
    eid = str(uuid4())
    payload = {'evaluation_id': eid, 'candidate_version_id': version['version_id'],
               'passed': passed, 'status': 'approved'}
    if promotion is not None:
        payload['promotion'] = promotion
    with registry.store.transaction() as db:
        db.execute('INSERT INTO evaluations VALUES (?,?,?,?)', (eid, None, json.dumps(payload), 'now'))
    return {'evaluation_id': eid}


def test_publish_requires_persisted_matching_evaluation(registry):
    v = registry.create_candidate('jwt', 'check expiration', scope_id='repo')
    with pytest.raises(ValueError, match='persisted'):
        registry.publish_version(v['version_id'], {'passed': True})
    failed = approve(registry, v, False)
    with pytest.raises(ValueError, match='approve'):
        registry.publish_version(v['version_id'], failed)
    assert registry.snapshot('repo') == {}
    registry.publish_version(v['version_id'], approve(registry, v))
    assert registry.snapshot('repo') == {v['skill_id']: v['version_id']}
    assert registry.snapshot('other') == {}


def test_cas_pinning_rollback_and_deprecation(registry):
    initial = registry.create_candidate('jwt', 'version one', scope_id='repo')
    registry.publish_version(initial['version_id'], approve(registry, initial))
    pinned = registry.snapshot('repo')
    first = registry.create_candidate('jwt', 'version two', scope_id='repo', skill_id=initial['skill_id'])
    second = registry.create_candidate('jwt', 'concurrent edit', scope_id='repo', skill_id=initial['skill_id'])
    registry.publish_version(first['version_id'], approve(registry, first))
    with pytest.raises(ValueError, match='changed'):
        registry.publish_version(second['version_id'], approve(registry, second))
    task = {'repository_id': 'repo', 'prompt': 'jwt bug', 'state': {'skill_snapshot': pinned}}
    assert registry.select(task)[0]['content'] == 'version one'
    registry.rollback(initial['skill_id'], initial['version_id'])
    assert registry.snapshot('repo') == pinned
    registry.deprecate(initial['skill_id'])
    assert registry.snapshot('repo') == {}
    assert registry.select(task)[0]['content'] == 'version one'


def test_integrity_scope_and_meta_exclusion(registry):
    meta = registry.create_candidate('strategy', 'extract carefully', level='meta')
    registry.publish_version(meta['version_id'], approve(registry, meta))
    assert registry.snapshot('repo') == {}
    general = registry.create_candidate('pytest', 'run pytest', level='general')
    registry.publish_version(general['version_id'], approve(registry, general))
    assert registry.select({'repository_id': 'repo', 'prompt': 'pytest'})
    assert registry.select({'repository_id': 'repo', 'prompt': 'pytest', 'config': {'skills': {'enabled': False}}}) == []
    from pathlib import Path
    Path(general['file_path']).write_text('tampered')
    with pytest.raises(ValueError, match='integrity'):
        registry.load(general['version_id'])


def test_selection_matches_normalized_context_and_explains_every_candidate(registry):
    skill = registry.create_candidate('release notes', 'summarize changes', level='general',
                                      metadata={'trigger_terms': ['release notes', 'ship']})
    registry.publish_version(skill['version_id'], approve(registry, skill))
    task = {'repository_id': 'repo', 'prompt': 'Prepare documentation for review',
            'state': {'skill_snapshot': registry.snapshot('repo')}}

    report = registry.select_with_reasons(task)
    assert report['selected'] == []
    assert report['decisions'][0]['reason'] == 'no_trigger_match'

    task['state']['plan'] = {'steps': [{'goal': 'Ship the prepared release notes'}]}
    report = registry.select_with_reasons(task)
    assert [item['version_id'] for item in report['selected']] == [skill['version_id']]
    decision = report['decisions'][0]
    assert decision['reason'] == 'trigger_match'
    assert decision['matches'] == [{'term': 'release notes', 'context': 'plan'},
                                   {'term': 'ship', 'context': 'plan'}]


def test_selection_reports_limit_and_keeps_scope_boundary(registry):
    allowed = registry.create_candidate('database migration', 'safe migration', level='project',
                                        scope_id='repo', metadata={'trigger_terms': ['migration']})
    wrong_scope = registry.create_candidate('other migration', 'other project', level='project',
                                            scope_id='elsewhere', metadata={'always_load': True})
    always = registry.create_candidate('always', 'always loaded', level='general',
                                       metadata={'always_load': True})
    for skill in (allowed, wrong_scope, always):
        registry.publish_version(skill['version_id'], approve(registry, skill))
    task = {'repository_id': 'repo', 'prompt': 'migration', 'config': {'skills': {'max_loaded_skills': 1}},
            'state': {'skill_snapshot': {allowed['skill_id']: allowed['version_id'],
                                         wrong_scope['skill_id']: wrong_scope['version_id'],
                                         always['skill_id']: always['version_id']}}}

    report = registry.select_with_reasons(task)
    assert [item['skill_id'] for item in report['selected']] == [allowed['skill_id']]
    reasons = {item['skill_id']: item['reason'] for item in report['decisions']}
    assert reasons == {allowed['skill_id']: 'trigger_match',
                       wrong_scope['skill_id']: 'scope_mismatch',
                       always['skill_id']: 'max_loaded_skills'}


def test_cannot_publish_other_candidate_or_rollback_draft(registry):
    one = registry.create_candidate('first', 'one', level='general')
    two = registry.create_candidate('second', 'two', level='general')
    with pytest.raises(ValueError, match='approve'):
        registry.publish_version(two['version_id'], approve(registry, one))
    with pytest.raises(ValueError, match='previously'):
        registry.rollback(one['skill_id'], one['version_id'])


def test_list_versions_and_parent_cas(registry):
    first = registry.create_candidate('jwt', 'version one', scope_id='repo', metadata={'trigger_terms': ['jwt']})
    registry.publish_version(first['version_id'], approve(registry, first))
    second = registry.create_candidate('jwt', 'version two', scope_id='repo', skill_id=first['skill_id'])

    versions = registry.list_versions()
    assert {version['version_id'] for version in versions} == {first['version_id'], second['version_id']}
    assert next(v for v in registry.list_versions(first['skill_id']) if v['version_id'] == first['version_id'])['metadata'] == {'trigger_terms': ['jwt']}
    assert registry.list_versions('missing') == []

    with pytest.raises(ValueError, match='candidate parent'):
        registry.publish_version(second['version_id'], approve(registry, second), expected_parent_version_id='unrelated')
    registry.publish_version(second['version_id'], approve(registry, second), expected_parent_version_id=first['version_id'])


def test_persisted_evaluation_identity_must_match(registry):
    version = registry.create_candidate('jwt', 'check expiration', scope_id='repo')
    eid = str(uuid4())
    with registry.store.transaction() as db:
        db.execute('INSERT INTO evaluations VALUES (?,?,?,?)', (eid, None, json.dumps({'evaluation_id': str(uuid4()), 'candidate_version_id': version['version_id'], 'passed': True, 'status': 'approved'}), 'now'))
    with pytest.raises(ValueError, match='approve'):
        registry.publish_version(version['version_id'], {'evaluation_id': eid})


def test_merge_publication_deprecates_sources_atomically_and_is_idempotent(registry):
    source_a = registry.create_candidate('source-a', 'instructions a', level='general')
    source_b = registry.create_candidate('source-b', 'instructions b', level='general')
    registry.publish_version(source_a['version_id'], approve(registry, source_a))
    registry.publish_version(source_b['version_id'], approve(registry, source_b))
    merged = registry.create_candidate('merged', 'combined instructions', level='general')
    source_versions = {source_a['skill_id']: source_a['version_id'],
                       source_b['skill_id']: source_b['version_id']}
    promotion = {'operation': 'MERGE', 'parent_version_id': None,
                 'source_skill_versions': source_versions}
    evaluation = approve(registry, merged, promotion=promotion)

    with pytest.raises(ValueError, match='must apply'):
        registry.publish_version(merged['version_id'], evaluation)
    registry.publish_version(merged['version_id'], evaluation, operation='MERGE',
                             expected_source_versions=source_versions)
    statuses = {skill['skill_id']: skill['status'] for skill in registry.list_skills()}
    assert statuses[source_a['skill_id']] == 'deprecated'
    assert statuses[source_b['skill_id']] == 'deprecated'
    assert registry.snapshot('repo') == {merged['skill_id']: merged['version_id']}

    # A retry after the database commit is safe when the complete post-state remains intact.
    registry.publish_version(merged['version_id'], evaluation, operation='MERGE',
                             expected_source_versions=source_versions)


@pytest.mark.parametrize('with_parent', [False, True])
def test_merge_rollback_restores_sources_and_previous_target(registry, with_parent):
    def active(name):
        version = registry.create_candidate(name, name, level='general')
        return registry.publish_version(version['version_id'], approve(registry, version))

    source_a, source_b = active('source-a'), active('source-b')
    parent = active('target') if with_parent else None
    merged = registry.create_candidate('target', 'combined', level='general',
                                       skill_id=parent['skill_id'] if parent else None)
    parent_id = parent['version_id'] if parent else None
    sources = {version['skill_id']: version['version_id'] for version in (source_a, source_b)}
    evaluation = approve(registry, merged, promotion={'operation': 'MERGE', 'parent_version_id': parent_id,
                                                      'source_skill_versions': sources})
    before = registry.snapshot('repo')
    registry.publish_version(merged['version_id'], evaluation, operation='MERGE', expected_source_versions=sources)
    receipt = registry.rollback_merge(merged['version_id'], evaluation['evaluation_id'])

    assert receipt['restored_source_versions'] == sources
    assert receipt['restored_target_version_id'] == parent_id
    assert registry.snapshot('repo') == before
    assert registry.load(merged['version_id'])['content'] == 'combined'
    assert registry.load(merged['version_id'])['status'] == 'superseded'
    if parent:
        assert registry.load(parent_id)['status'] == 'active'


def test_merge_rollback_refuses_changed_source_without_partial_restore(registry):
    def active(name):
        version = registry.create_candidate(name, name, level='general')
        return registry.publish_version(version['version_id'], approve(registry, version))

    source_a, source_b = active('source-a'), active('source-b')
    merged = registry.create_candidate('target', 'combined', level='general')
    sources = {version['skill_id']: version['version_id'] for version in (source_a, source_b)}
    evaluation = approve(registry, merged, promotion={'operation': 'MERGE', 'parent_version_id': None,
                                                      'source_skill_versions': sources})
    registry.publish_version(merged['version_id'], evaluation, operation='MERGE', expected_source_versions=sources)
    registry.rollback(source_b['skill_id'], source_b['version_id'])
    after_manual_change = registry.list_skills()

    with pytest.raises(ValueError, match='Merge source changed'):
        registry.rollback_merge(merged['version_id'], evaluation['evaluation_id'])

    assert registry.list_skills() == after_manual_change
    assert registry.load(merged['version_id'])['status'] == 'active'


def test_stale_merge_source_rejects_without_partial_pointer_changes(registry):
    source_a = registry.create_candidate('source-a', 'instructions a', level='general')
    source_b = registry.create_candidate('source-b', 'instructions b', level='general')
    registry.publish_version(source_a['version_id'], approve(registry, source_a))
    registry.publish_version(source_b['version_id'], approve(registry, source_b))
    merged = registry.create_candidate('merged', 'combined instructions', level='general')
    frozen_sources = {source_a['skill_id']: source_a['version_id'],
                      source_b['skill_id']: source_b['version_id']}
    promotion = {'operation': 'MERGE', 'parent_version_id': None,
                 'source_skill_versions': frozen_sources}
    evaluation = approve(registry, merged, promotion=promotion)

    newer_b = registry.create_candidate('source-b', 'newer instructions b', level='general',
                                        skill_id=source_b['skill_id'])
    registry.publish_version(newer_b['version_id'], approve(registry, newer_b))
    with pytest.raises(ValueError, match='Merge source active version changed'):
        registry.publish_version(merged['version_id'], evaluation, operation='MERGE',
                                 expected_source_versions=frozen_sources)

    statuses = {skill['skill_id']: skill for skill in registry.list_skills()}
    assert statuses[source_a['skill_id']]['status'] == 'active'
    assert statuses[source_a['skill_id']]['active_version_id'] == source_a['version_id']
    assert statuses[source_b['skill_id']]['status'] == 'active'
    assert statuses[source_b['skill_id']]['active_version_id'] == newer_b['version_id']
    assert statuses[merged['skill_id']]['active_version_id'] is None
    assert next(v for v in registry.list_versions(merged['skill_id']))['status'] == 'draft'


def test_deprecate_publication_is_atomic_and_idempotent(registry):
    original = registry.create_candidate('obsolete', 'old instructions', level='general')
    registry.publish_version(original['version_id'], approve(registry, original))
    retired = registry.create_candidate('obsolete', 'replacement state', level='general',
                                        skill_id=original['skill_id'],
                                        parent_version_id=original['version_id'])
    promotion = {'operation': 'DEPRECATE', 'parent_version_id': original['version_id'],
                 'source_skill_versions': {}}
    evaluation = approve(registry, retired, promotion=promotion)

    registry.publish_version(retired['version_id'], evaluation,
                             expected_parent_version_id=original['version_id'],
                             operation='DEPRECATE')
    skill = next(s for s in registry.list_skills() if s['skill_id'] == original['skill_id'])
    assert skill['status'] == 'deprecated'
    assert skill['active_version_id'] == retired['version_id']
    assert next(v for v in registry.list_versions(original['skill_id'])
                if v['version_id'] == retired['version_id'])['status'] == 'active'

    registry.publish_version(retired['version_id'], evaluation,
                             expected_parent_version_id=original['version_id'],
                             operation='DEPRECATE')


def test_selection_matches_core_domain_tokens_for_multiword_triggers(registry):
    """验证多词领域触发词在非连续表达下可通过核心词共现命中，且无关提示词仍不误配。"""
    validation_skill = registry.create_candidate(
        'strict-validator', 'validate exact types', level='project', scope_id='val-repo',
        metadata={'trigger_terms': ['exact int validation', 'TypeError and ValueError', 'inclusive range checks']},
    )
    api_skill = registry.create_candidate(
        'api-compat', 'preserve signature', level='project', scope_id='api-repo',
        metadata={'trigger_terms': ['keyword-only argument', 'preserve existing fields']},
    )
    registry.publish_version(validation_skill['version_id'], approve(registry, validation_skill))
    registry.publish_version(api_skill['version_id'], approve(registry, api_skill))

    val_task = {
        'repository_id': 'val-repo',
        'prompt': 'size_bytes must be an exact int from 1 through 100 inclusive; raise TypeError for bad types and ValueError for out-of-range values.',
        'state': {'skill_snapshot': registry.snapshot('val-repo')},
    }
    val_report = registry.select_with_reasons(val_task)
    assert [item['version_id'] for item in val_report['selected']] == [validation_skill['version_id']]
    assert val_report['decisions'][0]['score'] == 3

    api_task = {
        'repository_id': 'api-repo',
        'prompt': 'Add keyword-only version=None and preserve the existing default output.',
        'state': {'skill_snapshot': registry.snapshot('api-repo')},
    }
    api_report = registry.select_with_reasons(api_task)
    assert [item['version_id'] for item in api_report['selected']] == [api_skill['version_id']]
    assert api_report['decisions'][0]['score'] == 2

    unrelated_task = {
        'repository_id': 'api-repo',
        'prompt': 'Format markdown changelog table.',
        'state': {'skill_snapshot': registry.snapshot('api-repo')},
    }
    assert registry.select_with_reasons(unrelated_task)['selected'] == []


def test_cjk_and_mixed_trigger_terms_match_chinese_prompts(registry):
    """验证中文及中英混合触发词在中文任务描述下可通过子串或首尾双字共现命中。"""
    cjk_skill = registry.create_candidate(
        'invoice-validator', '统一发票校验与错误信封', level='project', scope_id='repo_api',
        metadata={'trigger_terms': ['布尔整数拒绝', '输入校验', 'billing_api/service.py']},
    )
    registry.publish_version(cjk_skill['version_id'], approve(registry, cjk_skill))

    task = {
        'repository_id': 'repo_api',
        'prompt': '修复导出预览接口的输入校验缺陷：布尔值不能当作整数通过，且需拒绝空白字符串。',
        'state': {'skill_snapshot': registry.snapshot('repo_api')},
    }
    report = registry.select_with_reasons(task)
    assert [item['version_id'] for item in report['selected']] == [cjk_skill['version_id']]
    assert report['decisions'][0]['score'] == 2

