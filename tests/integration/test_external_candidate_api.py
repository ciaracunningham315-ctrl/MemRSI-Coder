"""Offline contract tests for the external-candidate API, not NSRR task outcomes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from evocode.api import create_app
from evocode.app import Application
from evocode.config import AppConfig


def _candidate_inputs(tmp_path, monkeypatch, *, candidate_id='nsrr_dup_headers'):
    audit_root = tmp_path / 'benchmark'
    audit_task_id = candidate_id.replace('nsrr_', 'nsrr_audit_', 1)
    fixture_root = audit_root / '.runtime' / 'nsrr_fixtures'
    fixture_dir = fixture_root / audit_task_id
    repo = fixture_dir
    repo.mkdir(parents=True)
    (repo / 'module.py').write_text('VALUE = 1\n', encoding='utf-8')
    code_source = audit_root / 'sources' / audit_task_id
    code_source.mkdir(parents=True)
    (code_source / 'module.py').write_text('VALUE = 1\n', encoding='utf-8')
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run([
        'git', '-C', str(repo), '-c', 'user.name=Test', '-c',
        'user.email=test@example.invalid', 'commit', '-qm', 'fixture',
    ], check=True)
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    task_root = audit_root / 'private' / 'gate_audit' / 'tasks' / audit_task_id
    oracle = task_root / 'tests' / 'test_acceptance.py'
    oracle.parent.mkdir(parents=True)
    oracle.write_text('assert True\n', encoding='utf-8')
    reference_dir = task_root / 'reference'
    reference_dir.mkdir()
    (reference_dir / 'README.md').write_text('Frozen reference.\n', encoding='utf-8')
    private_tests_dir = task_root / 'private_tests'
    private_tests_dir.mkdir()
    (private_tests_dir / 'trusted_tests.json').write_text('{"cases": []}\n', encoding='utf-8')
    task_file = task_root / 'task.json'
    task_file.write_text('{"title": "offline fixture"}\n', encoding='utf-8')

    baseline_skill = '# Baseline skill\nKeep the existing behavior.\n'
    candidate_skill = '# Candidate skill\nUse the improved behavior.\n'
    baseline_path = audit_root / 'gate_cases' / candidate_id / 'baseline' / 'SKILL.md'
    candidate_path = audit_root / 'gate_cases' / candidate_id / 'candidate' / 'SKILL.md'
    baseline_path.parent.mkdir(parents=True)
    candidate_path.parent.mkdir(parents=True)
    baseline_path.write_text(baseline_skill, encoding='utf-8')
    candidate_path.write_text(candidate_skill, encoding='utf-8')

    acceptance_command = ['python', '-I', '-B', '/workspace/tests/test_acceptance.py']
    acceptance_files = ['tests/test_acceptance.py']
    oracle_relative = oracle.relative_to(audit_root).as_posix()
    scope = 'offline-contract-repo'
    fixed_input_fingerprint = hashlib.sha256(b'offline fixture inputs').hexdigest()
    reference_relative = reference_dir.relative_to(audit_root).as_posix()
    private_tests_relative = private_tests_dir.relative_to(audit_root).as_posix()
    task_relative = task_file.relative_to(audit_root).as_posix()
    task_manifest_specs = [
        {'repository_id': scope, 'commit': commit,
         'prompt': 'Offline contract fixture; this is not an NSRR audit task.',
         'acceptance_command': acceptance_command, 'acceptance_files': acceptance_files,
         'protected_acceptance_paths': {'tests/test_acceptance.py': oracle_relative}},
        {'repository_id': scope, 'commit': commit,
         'prompt': 'Second offline contract fixture task; not an NSRR audit task.',
         'acceptance_command': acceptance_command, 'acceptance_files': acceptance_files,
         'protected_acceptance_paths': {'tests/test_acceptance.py': oracle_relative}},
    ]
    fixture_manifest = {
        'candidate_id': candidate_id,
        'audit_task_id': audit_task_id,
        'repository_id': scope,
        'code_dir': code_source.relative_to(audit_root).as_posix(),
        'reference_dir': reference_relative,
        'private_tests_dir': private_tests_relative,
        'task_file': task_relative,
        'fixed_input_fingerprint': fixed_input_fingerprint,
        'task_specs': task_manifest_specs,
    }
    fixture_manifest_path = task_root / 'fixture_manifest.json'
    fixture_manifest_path.write_text(json.dumps(fixture_manifest, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')

    frozen_inputs = [baseline_path, candidate_path, oracle, task_file,
                     private_tests_dir / 'trusted_tests.json', reference_dir / 'README.md',
                     code_source / 'module.py', fixture_manifest_path]
    lock_path = audit_root / 'private' / 'gate_audit' / 'freeze_manifest.json'
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    def write_lock():
        lock = {'inputs': {
            path.relative_to(audit_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in frozen_inputs
        }}
        lock_path.write_text(json.dumps(lock, sort_keys=True) + '\n', encoding='utf-8')

    write_lock()
    monkeypatch.setenv('EVOCODE_NSRR_AUDIT_ROOT', str(audit_root))
    monkeypatch.setenv('EVOCODE_NSRR_FIXTURE_ROOT', str(fixture_root))
    candidate = {
        'candidate_id': candidate_id,
        'repository_scope': scope,
        'baseline_skill': baseline_skill,
        'candidate_skill': candidate_skill,
        'task_specs': [{
            **task_spec,
            'audit_task_id': audit_task_id,
            'fixed_input_fingerprint': fixed_input_fingerprint,
            'reference_dir': str(audit_root / reference_relative),
            'private_tests_dir': str(audit_root / private_tests_relative),
            'task_file': str(audit_root / task_relative),
            'repo_path': str(repo),
            'protected_acceptance_paths': {
                target: str(audit_root / source)
                for target, source in task_spec['protected_acceptance_paths'].items()
            },
        } for task_spec in task_manifest_specs],
    }
    return candidate, fixture_root, write_lock, oracle


class _MockEvaluator:
    """Persist deterministic reports without executing a model or a sandbox."""

    def __init__(self, store):
        self.store = store
        self.calls = []
        self.passed_override = None

    async def compare(self, run_id, candidate_version_id, task_specs, baseline, candidate):
        assert 1 <= len(task_specs) <= 2
        self.calls.append((run_id, candidate_version_id, task_specs, baseline, candidate))
        passed = self.passed_override
        if passed is None:
            passed = 'improved' in candidate['skill_versions'][0]['content']
        report = {
            'evaluation_id': str(uuid.uuid4()),
            'run_id': run_id,
            'candidate_version_id': candidate_version_id,
            'passed': passed,
            'status': 'approved' if passed else 'needs_review',
            'rows': [],
        }
        with self.store.transaction() as conn:
            conn.execute(
                'INSERT INTO evaluations(evaluation_id,run_id,payload_json,created_at) VALUES(?,?,?,?)',
                (report['evaluation_id'], run_id, json.dumps(report), 'offline-test'),
            )
        return report


def _application(tmp_path):
    config = AppConfig(data_dir=str(tmp_path / 'state'))
    config.raw_rag.enabled = False
    config.raw_rag.index_when_disabled = False
    config.raw_rag.background_indexing = False
    config.evolution.enabled = False
    app = Application(config)
    evaluator = _MockEvaluator(app.store)
    app.evolution.evaluator = evaluator
    return app, evaluator


def _post_body(candidate, audit_fixture_root, *, batch_id='offline-batch', collection='frozen_contract_memory'):
    return {
        'batch_id': batch_id,
        'frozen_memory_collection': collection,
        'audit_fixture_root': str(audit_fixture_root),
        'candidates': [candidate],
    }


def test_external_candidate_api_polls_real_persisted_evaluation_and_is_idempotent(tmp_path, monkeypatch):
    # This fixture checks API and persistence mechanics only. It is not one of the frozen NSRR workloads.
    candidate, fixture_root, write_lock, oracle = _candidate_inputs(tmp_path, monkeypatch)
    app, evaluator = _application(tmp_path)
    registry_before = app.skills.list_versions()
    with TestClient(create_app(application=app)) as client:
        response = client.post('/api/evolution/external-candidates/evaluate', json=_post_body(candidate, fixture_root))
        assert response.status_code == 202, response.text
        queued = response.json()
        assert queued['status_url'] == '/api/evolution/external-candidates/offline-batch'
        run_id = queued['runs'][0]['run_id']
        for _ in range(100):
            batch = client.get(queued['status_url']).json()
            if batch['status'] in ('completed', 'failed'):
                break
            time.sleep(0.01)
        assert batch['status'] == 'completed', batch
        record = batch['records'][0]
        assert record['source'] == 'nsrr_candidate_evaluation_api_record'
        assert record['candidate_id'] == 'nsrr_dup_headers'
        assert record['run_id'] == run_id
        assert record['evaluation_passed'] is record['evaluation']['passed'] is True
        assert record['decision'] == 'accepted'
        assert len(evaluator.calls) == 1

        run = client.get(f'/api/evolution/{run_id}').json()
        assert run['kind'] == 'external_task'
        assert run['payload']['candidate']['nsrr_candidate_id'] == 'nsrr_dup_headers'
        assert run['payload']['evaluation']['evaluation_id'] == record['evaluation_id']
        with app.store.transaction() as conn:
            persisted = conn.execute('SELECT payload_json FROM evaluations WHERE evaluation_id=?',
                                     (record['evaluation_id'],)).fetchone()
        assert json.loads(persisted['payload_json'])['nsrr_candidate_id'] == 'nsrr_dup_headers'
        assert app.skills.list_versions() == registry_before

        retry = client.post('/api/evolution/external-candidates/evaluate', json=_post_body(candidate, fixture_root))
        assert retry.status_code == 202
        assert retry.json()['runs'][0]['run_id'] == run_id
        assert len(evaluator.calls) == 1

        oracle.write_text('assert True\n# trusted fixture changed\n', encoding='utf-8')
        write_lock()
        conflict = client.post('/api/evolution/external-candidates/evaluate', json=_post_body(candidate, fixture_root))
        assert conflict.status_code == 400
        assert 'different inputs' in conflict.json()['detail']

        with pytest.raises(ValueError, match='evaluation-only'):
            app.evolution.publish(run_id)
        with pytest.raises(ValueError, match='evaluation-only'):
            app.evolution.rollback_merge(run_id)


def test_external_candidate_api_maps_false_persisted_evaluation_to_blocked(tmp_path, monkeypatch):
    candidate, fixture_root, _, _ = _candidate_inputs(tmp_path, monkeypatch)
    app, evaluator = _application(tmp_path)
    evaluator.passed_override = False
    with TestClient(create_app(application=app)) as client:
        response = client.post('/api/evolution/external-candidates/evaluate', json=_post_body(candidate, fixture_root))
        assert response.status_code == 202
        status_url = response.json()['status_url']
        for _ in range(100):
            batch = client.get(status_url).json()
            if batch['status'] in ('completed', 'failed'):
                break
            time.sleep(0.01)
        assert batch['status'] == 'completed'
        record = batch['records'][0]
        assert record['evaluation_passed'] is record['evaluation']['passed'] is False
        assert record['decision'] == 'blocked'
        assert record['status'] == 'needs_review'


def test_external_candidate_api_derives_task_provenance_from_frozen_manifest(tmp_path, monkeypatch):
    candidate, fixture_root, _, _ = _candidate_inputs(tmp_path, monkeypatch)
    task_id = candidate['task_specs'][0]['audit_task_id']
    for spec in candidate['task_specs']:
        spec['task_id'] = spec.pop('audit_task_id')
        for field in ('reference_dir', 'private_tests_dir', 'task_file'):
            spec.pop(field)
    app, evaluator = _application(tmp_path)
    with TestClient(create_app(application=app)) as client:
        response = client.post('/api/evolution/external-candidates/evaluate', json=_post_body(candidate, fixture_root))
        assert response.status_code == 202, response.text
        status_url = response.json()['status_url']
        for _ in range(100):
            batch = client.get(status_url).json()
            if batch['status'] in ('completed', 'failed'):
                break
            time.sleep(0.01)
        assert batch['status'] == 'completed', batch

    normalized = evaluator.calls[0][2]
    assert len(normalized) == 2
    for spec in normalized:
        assert spec['task_id'] == spec['audit_task_id'] == task_id
        assert spec['fixed_input_fingerprint'] == candidate['task_specs'][0]['fixed_input_fingerprint']
        assert spec['reference_dir'].endswith(f'/private/gate_audit/tasks/{task_id}/reference')
        assert spec['private_tests_dir'].endswith(f'/private/gate_audit/tasks/{task_id}/private_tests')
        assert spec['task_file'].endswith(f'/private/gate_audit/tasks/{task_id}/task.json')


def test_external_candidate_api_accepts_runner_materializer_task_spec_shape(tmp_path, monkeypatch):
    candidate, fixture_root, write_lock, _ = _candidate_inputs(tmp_path, monkeypatch)
    audit_root = fixture_root.parent.parent
    task_id = candidate['task_specs'][0]['audit_task_id']
    task_spec = candidate['task_specs'][0]
    task_file = tmp_path / 'benchmark' / 'private' / 'gate_audit' / 'tasks' / task_id / 'task.json'
    task_file.write_text(task_spec['prompt'], encoding='utf-8')
    manifest_path = task_file.parent / 'fixture_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest.pop('task_specs')
    manifest.update({
        'repo_template': manifest['code_dir'],
        'commit': task_spec['commit'],
        'prompt': task_spec['prompt'],
        'acceptance_command': task_spec['acceptance_command'],
        'acceptance_files': task_spec['acceptance_files'],
        'protected_acceptance_paths': {
            target: source.relative_to(audit_root).as_posix()
            for target, source in ((target, Path(path)) for target, path in task_spec['protected_acceptance_paths'].items())
        },
        'baseline_skill': f"gate_cases/{candidate['candidate_id']}/baseline/SKILL.md",
        'candidate_skill': f"gate_cases/{candidate['candidate_id']}/candidate/SKILL.md",
        'audit_use_only': True,
        'independent_of_main_tasks': True,
        'main_task_score_eligible': False,
    })
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')
    write_lock()

    runner_spec = {key: value for key, value in task_spec.items()
                   if key not in ('audit_task_id', 'reference_dir', 'private_tests_dir', 'task_file')}
    runner_spec['task_id'] = task_id
    candidate['task_specs'] = [runner_spec]
    app, evaluator = _application(tmp_path)
    with TestClient(create_app(application=app)) as client:
        response = client.post('/api/evolution/external-candidates/evaluate', json=_post_body(candidate, fixture_root))
        assert response.status_code == 202, response.text
        status_url = response.json()['status_url']
        for _ in range(100):
            batch = client.get(status_url).json()
            if batch['status'] in ('completed', 'failed'):
                break
            time.sleep(0.01)
        assert batch['status'] == 'completed', batch

    normalized = evaluator.calls[0][2][0]
    assert normalized['task_id'] == normalized['audit_task_id'] == task_id
    assert normalized['prompt'] == task_file.read_text(encoding='utf-8')
    assert normalized['reference_dir'] == str(task_file.parent / 'reference')
    assert normalized['private_tests_dir'] == str(task_file.parent / 'private_tests')
    assert normalized['task_file'] == str(task_file)


def test_external_candidate_api_rejects_self_reported_task_provenance_paths(tmp_path, monkeypatch):
    candidate, fixture_root, _, _ = _candidate_inputs(tmp_path, monkeypatch)
    candidate['task_specs'][0]['task_file'] = str(tmp_path / 'untrusted-task.md')
    app, evaluator = _application(tmp_path)
    with TestClient(create_app(application=app)) as client:
        response = client.post('/api/evolution/external-candidates/evaluate', json=_post_body(candidate, fixture_root))
        assert response.status_code == 400
        assert 'task_file differs from the frozen fixture manifest' in response.json()['detail']
        assert evaluator.calls == []


@pytest.mark.parametrize('mutation,expected', [
    (lambda c: c.update(candidate_id='../invalid'), 'candidate_id'),
    (lambda c: c.update(baseline_skill='  '), 'baseline_skill'),
    (lambda c: c['task_specs'][0].update(repository_id='other-repo'), 'repository_id'),
    (lambda c: c['task_specs'][0].pop('protected_acceptance_paths'), 'trusted acceptance'),
])
def test_external_candidate_api_rejects_untrusted_inputs_before_evaluation(tmp_path, monkeypatch, mutation, expected):
    candidate, fixture_root, _, _ = _candidate_inputs(tmp_path, monkeypatch)
    mutation(candidate)
    app, evaluator = _application(tmp_path)
    with TestClient(create_app(application=app)) as client:
        response = client.post('/api/evolution/external-candidates/evaluate', json=_post_body(candidate, fixture_root))
        assert response.status_code == 400, response.text
        assert expected in response.json()['detail']
        assert evaluator.calls == []
        assert app.evolution.list_runs() == []


def test_external_candidate_api_rejects_missing_frozen_collection(tmp_path, monkeypatch):
    candidate, fixture_root, _, _ = _candidate_inputs(tmp_path, monkeypatch)
    app, evaluator = _application(tmp_path)
    with TestClient(create_app(application=app)) as client:
        response = client.post('/api/evolution/external-candidates/evaluate', json=_post_body(candidate, fixture_root, collection='../invalid'))
        assert response.status_code == 400
        assert 'frozen_memory_collection' in response.json()['detail']
        assert evaluator.calls == []


def test_external_candidate_api_fails_closed_without_trust_roots(tmp_path, monkeypatch):
    candidate, fixture_root, _, _ = _candidate_inputs(tmp_path, monkeypatch)
    monkeypatch.delenv('EVOCODE_NSRR_AUDIT_ROOT')
    app, evaluator = _application(tmp_path)
    with TestClient(create_app(application=app)) as client:
        response = client.post('/api/evolution/external-candidates/evaluate', json=_post_body(candidate, fixture_root))
        assert response.status_code == 400
        assert 'trust roots' in response.json()['detail']
        assert evaluator.calls == []
        assert app.evolution.list_runs() == []


def test_external_candidate_api_rejects_repo_outside_configured_fixture_root(tmp_path, monkeypatch):
    candidate, fixture_root, _, _ = _candidate_inputs(tmp_path, monkeypatch)
    outside = tmp_path / 'outside-repo'
    outside.mkdir()
    (outside / 'module.py').write_text('VALUE = 1\n', encoding='utf-8')
    subprocess.run(['git', 'init', '-q', str(outside)], check=True)
    subprocess.run(['git', '-C', str(outside), 'add', '.'], check=True)
    subprocess.run([
        'git', '-C', str(outside), '-c', 'user.name=Test', '-c',
        'user.email=test@example.invalid', 'commit', '-qm', 'outside',
    ], check=True)
    commit = subprocess.check_output(['git', '-C', str(outside), 'rev-parse', 'HEAD'], text=True).strip()
    candidate['task_specs'][0]['repo_path'] = str(outside)
    candidate['task_specs'][0]['commit'] = commit
    app, evaluator = _application(tmp_path)
    with TestClient(create_app(application=app)) as client:
        response = client.post('/api/evolution/external-candidates/evaluate', json=_post_body(candidate, fixture_root))
        assert response.status_code == 400
        assert 'fixture directory' in response.json()['detail']
        assert evaluator.calls == []
        assert app.evolution.list_runs() == []


def test_external_candidate_api_rejects_task_specs_spanning_git_roots(tmp_path, monkeypatch):
    candidate, fixture_root, _, _ = _candidate_inputs(tmp_path, monkeypatch)
    outside = tmp_path / 'another-repo'
    outside.mkdir()
    (outside / 'module.py').write_text('VALUE = 2\n', encoding='utf-8')
    subprocess.run(['git', 'init', '-q', str(outside)], check=True)
    subprocess.run(['git', '-C', str(outside), 'add', '.'], check=True)
    subprocess.run([
        'git', '-C', str(outside), '-c', 'user.name=Test', '-c',
        'user.email=test@example.invalid', 'commit', '-qm', 'second repo',
    ], check=True)
    candidate['task_specs'][1]['repo_path'] = str(outside)
    candidate['task_specs'][1]['commit'] = subprocess.check_output(
        ['git', '-C', str(outside), 'rev-parse', 'HEAD'], text=True).strip()
    app, evaluator = _application(tmp_path)
    with TestClient(create_app(application=app)) as client:
        response = client.post('/api/evolution/external-candidates/evaluate', json=_post_body(candidate, fixture_root))
        assert response.status_code == 400
        assert 'fixture directory' in response.json()['detail']
        assert evaluator.calls == []


def test_external_candidate_api_rejects_oracle_outside_manifested_audit_root(tmp_path, monkeypatch):
    candidate, fixture_root, _, _ = _candidate_inputs(tmp_path, monkeypatch)
    outside = tmp_path / 'secret' / 'test_acceptance.py'
    outside.parent.mkdir()
    outside.write_text('assert True\n', encoding='utf-8')
    candidate['task_specs'][0]['protected_acceptance_paths']['tests/test_acceptance.py'] = str(outside)
    app, evaluator = _application(tmp_path)
    with TestClient(create_app(application=app)) as client:
        response = client.post('/api/evolution/external-candidates/evaluate', json=_post_body(candidate, fixture_root))
        assert response.status_code == 400
        assert 'fixture manifest' in response.json()['detail']
        assert evaluator.calls == []
        assert app.evolution.list_runs() == []


@pytest.mark.asyncio
async def test_external_candidate_run_fails_closed_when_trusted_oracle_changes_after_queue(tmp_path, monkeypatch):
    candidate, fixture_root, _, oracle = _candidate_inputs(tmp_path, monkeypatch)
    app, evaluator = _application(tmp_path)
    runs = app.evolution.queue_external_candidate_batch(
        'tamper-batch', 'frozen_contract_memory', str(fixture_root), [candidate])
    oracle.write_text('assert False\n', encoding='utf-8')

    await app.evolution.evaluate_external_candidate_runs([runs[0]['run_id']])
    stored = app.evolution.get_run(runs[0]['run_id'])
    assert stored['status'] == 'failed'
    assert 'SHA-256 changed' in stored['payload']['error']
    assert evaluator.calls == []
    batch = app.evolution.get_external_candidate_batch('tamper-batch')
    assert batch['status'] == 'failed'
    assert 'evaluation_passed' not in batch['records'][0]
    await app.close()


def test_external_candidate_batch_marks_orphaned_evaluation_interrupted_without_retry(tmp_path, monkeypatch):
    candidate, fixture_root, _, _ = _candidate_inputs(tmp_path, monkeypatch)
    app, evaluator = _application(tmp_path)
    runs = app.evolution.queue_external_candidate_batch(
        'orphaned-batch', 'frozen_contract_memory', str(fixture_root), [candidate])
    with app.store.transaction() as conn:
        conn.execute("UPDATE evolution_runs SET status='evaluating' WHERE run_id=?", (runs[0]['run_id'],))

    with TestClient(create_app(application=app)) as client:
        response = client.get('/api/evolution/external-candidates/orphaned-batch')
        assert response.status_code == 200
        batch = response.json()
        assert batch['status'] == 'failed'
        assert batch['records'][0]['status'] == 'interrupted'
        assert batch['records'][0]['reason'] == 'evaluator_process_lost_or_unknown'

        retry = client.post('/api/evolution/external-candidates/evaluate', json=_post_body(
            candidate, fixture_root, batch_id='orphaned-batch'))
        assert retry.status_code == 202
        assert retry.json()['status'] == 'failed'
        assert evaluator.calls == []
