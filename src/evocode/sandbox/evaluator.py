"""Paired execution with immutable inputs and isolated checkout copies."""
from __future__ import annotations

import asyncio
import copy
import json
import math
from pathlib import Path
import shutil
import sqlite3
from contextlib import contextmanager
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def _backup_sqlite(source_path: Path, destination_path: Path) -> bool:
    """Copy a consistent SQLite snapshot, including committed WAL pages."""
    if not source_path.is_file():
        return False
    source = sqlite3.connect(source_path.resolve().as_uri() + '?mode=ro', uri=True)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return True


class SandboxEvaluator:
    def __init__(self, store, config, runner=None):
        self.store, self.config, self.runner = store, config, runner

    def _is_meta_candidate(self, candidate_version_id):
        with self.store.transaction() as conn:
            row = conn.execute(
                'SELECT s.level FROM skill_versions v JOIN skills s USING(skill_id) '
                'WHERE v.version_id=?', (candidate_version_id,),
            ).fetchone()
        return bool(row and row['level'] == 'meta')

    def _candidate_project_scope(self, candidate_version_id):
        """获取项目级候选技能的 scope_id，用于过滤跨仓库无关评测行。"""
        with self.store.transaction() as conn:
            row = conn.execute(
                'SELECT s.level, s.scope_id FROM skill_versions v JOIN skills s USING(skill_id) '
                'WHERE v.version_id=?', (candidate_version_id,),
            ).fetchone()
        if row and row['level'] == 'project' and row['scope_id']:
            return row['scope_id']
        return None

    @staticmethod
    def _meta_injection_comparison(rows, task_specs, baseline_snapshot, candidate_snapshot, metric):
        """Select only same-repository rows with verified dual-arm exposure."""
        baseline_versions = baseline_snapshot.get('skill_versions') or []
        candidate_versions = candidate_snapshot.get('skill_versions') or []
        comparable_indices = []
        metric_indices = []
        diagnostics = []
        incomplete = False

        def derived_for_scope(versions, scope):
            return [version for version in versions
                    if version.get('level') == 'project'
                    and version.get('status') == 'draft'
                    and version.get('scope_id') == scope]

        def evidence_for(result, version):
            expected_hash = version.get('content_sha256')
            source_content = version.get('content')
            if not expected_hash and isinstance(source_content, str):
                import hashlib
                expected_hash = hashlib.sha256(source_content.encode()).hexdigest()
            matches = [item for item in result.get('coder_skill_injection_evidence', [])
                       if isinstance(item, dict)
                       and item.get('injection_verified') is True
                       and item.get('model_call_observed') is True
                       and item.get('content_sha256') == expected_hash
                       and item.get('version_id') and item.get('context_id')]
            return expected_hash, matches

        for index, (row, spec) in enumerate(zip(rows, task_specs)):
            scope = spec.get('repository_id')
            base_skills = derived_for_scope(baseline_versions, scope)
            candidate_skills = derived_for_scope(candidate_versions, scope)
            diagnostic = {
                'row_index': index,
                'task_id': spec.get('task_id') or spec.get('id'),
                'repository_id': scope,
                'comparable': False,
                'metric_included': False,
                'excluded_reason': None,
            }
            task_id_str = str(spec.get('task_id') or spec.get('id') or '')
            if spec.get('gate_kind') == 'regression' or '_replay_' in task_id_str:
                diagnostic['excluded_reason'] = 'regression_replay_task'
                diagnostics.append(diagnostic)
                continue
            if not base_skills and not candidate_skills:
                diagnostic['excluded_reason'] = 'project_scope_mismatch'
                diagnostics.append(diagnostic)
                continue
            if len(base_skills) != 1 or len(candidate_skills) != 1:
                diagnostic['excluded_reason'] = (
                    'missing_baseline_derived_skill' if not base_skills else
                    'missing_candidate_derived_skill' if not candidate_skills else
                    'ambiguous_derived_skills_for_repository'
                )
                incomplete = True
                diagnostics.append(diagnostic)
                continue

            baseline_skill, candidate_skill = base_skills[0], candidate_skills[0]
            base_hash, base_evidence = evidence_for(row['baseline'], baseline_skill)
            candidate_hash, candidate_evidence = evidence_for(row['candidate'], candidate_skill)
            diagnostic['baseline_derived_skill'] = {
                'source_version_id': baseline_skill.get('version_id'),
                'source_content_sha256': base_hash,
                'injected_version_ids': sorted({item['version_id'] for item in base_evidence}),
                'injected_context_ids': sorted({item['context_id'] for item in base_evidence}),
            }
            diagnostic['candidate_derived_skill'] = {
                'source_version_id': candidate_skill.get('version_id'),
                'source_content_sha256': candidate_hash,
                'injected_version_ids': sorted({item['version_id'] for item in candidate_evidence}),
                'injected_context_ids': sorted({item['context_id'] for item in candidate_evidence}),
            }
            if not base_hash or not base_evidence:
                diagnostic['excluded_reason'] = 'baseline_skill_not_verified_in_coder_model_context'
            elif not candidate_hash or not candidate_evidence:
                diagnostic['excluded_reason'] = 'candidate_skill_not_verified_in_coder_model_context'
            else:
                diagnostic['comparable'] = True
                comparable_indices.append(index)
                if all(isinstance(row[side].get(metric), (int, float))
                       and not isinstance(row[side].get(metric), bool)
                       and math.isfinite(row[side][metric])
                       and row[side][metric] >= 0
                       for side in ('baseline', 'candidate')):
                    diagnostic['metric_included'] = True
                    metric_indices.append(index)
                else:
                    diagnostic['excluded_reason'] = 'missing_comparable_metric'
                    incomplete = True
            if diagnostic['excluded_reason'] and diagnostic['excluded_reason'] not in ('project_scope_mismatch', 'regression_replay_task'):
                incomplete = True
            diagnostics.append(diagnostic)

        return {
            'task_count': len(rows),
            'comparable_pair_count': len(comparable_indices),
            'scored_pair_count': len(metric_indices),
            'scored_row_indices': metric_indices,
            'excluded_rows': [item for item in diagnostics if not item['metric_included']],
            'rows': diagnostics,
            'evidence_complete': not incomplete,
            'has_comparable_pair': bool(metric_indices),
        }

    async def _execute(self, spec, snapshot):
        if not spec.get('acceptance_command'):
            return {'passed': False, 'error': 'No independent acceptance command', 'unverified': True}
        if self.runner is None:
            return {'passed': False, 'error': 'Isolated runtime runner is unavailable', 'unverified': True}
        repo = Path(spec['repo_path']).resolve()
        with tempfile.TemporaryDirectory(prefix='evocode-eval-') as temp:
            workspace = Path(temp) / 'workspace'
            # Independent clone includes Git metadata without sharing mutable worktree state.
            try:
                subprocess.run(['git', 'clone', '--no-hardlinks', '--quiet', str(repo), str(workspace)], check=True, capture_output=True, timeout=60)
                commit = spec.get('commit') or subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
                subprocess.run(['git', '-C', str(workspace), 'checkout', '--detach', '--quiet', commit], check=True, capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as exc:
                return {'passed': False, 'error': f'Checkout failed: {exc}', 'unverified': True}
            acceptance = snapshot.get('acceptance_files')
            protected_paths = spec.get('protected_acceptance_paths') or {}
            if not isinstance(protected_paths, dict):
                return {'passed': False, 'error': 'Protected acceptance paths must be a path-to-file mapping', 'unverified': True}
            if protected_paths:
                acceptance = {}
                for relative, source_name in protected_paths.items():
                    if not isinstance(relative, str) or not isinstance(source_name, str):
                        return {'passed': False, 'error': 'Protected acceptance paths must contain strings', 'unverified': True}
                    source = Path(source_name)
                    if not source.is_absolute() or source.is_symlink():
                        return {'passed': False, 'error': 'Oracle files must be trusted absolute non-symlink paths', 'unverified': True}
                    source = source.resolve()
                    if source.is_relative_to(workspace.resolve()) or source.is_relative_to(repo) or not source.is_file():
                        return {'passed': False, 'error': 'Oracle files must exist outside the source and evaluation checkouts', 'unverified': True}
                    try:
                        acceptance[relative] = source.read_text()
                    except (OSError, UnicodeError) as exc:
                        return {'passed': False, 'error': f'Could not read protected oracle file: {exc}', 'unverified': True}
            if acceptance is None:
                acceptance = {}
                protected = spec.get('acceptance_files') or [str(p.relative_to(workspace)) for p in workspace.rglob('*') if p.is_file() and not p.is_symlink() and (p.name.startswith('test_') or 'tests' in p.relative_to(workspace).parts)]
                for relative in protected:
                    path = (workspace / relative).resolve()
                    if not path.is_relative_to(workspace.resolve()) or not path.is_file():
                        return {'passed': False, 'error': 'Invalid protected acceptance path', 'unverified': True}
                    try:
                        acceptance[relative] = path.read_text()
                    except UnicodeError:
                        return {'passed': False, 'error': 'Acceptance files must be text', 'unverified': True}
            if not isinstance(acceptance, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in acceptance.items()):
                return {'passed': False, 'error': 'Protected acceptance files must be a path-to-text mapping', 'unverified': True}
            # Reject paths that escape the frozen repository and require test-like paths.
            for relative in acceptance:
                path = Path(relative)
                if '\\' in relative or path.is_absolute() or '..' in path.parts or not (path.name.startswith('test_') or 'tests' in path.parts):
                    return {'passed': False, 'error': 'Invalid protected acceptance path', 'unverified': True}
            if not acceptance:
                return {'passed': False, 'error': 'No immutable acceptance files identified', 'unverified': True}
            isolated = copy.deepcopy(snapshot)
            isolated['acceptance_files'] = copy.deepcopy(acceptance)
            isolated['oracle_only_acceptance'] = bool(protected_paths)
            isolated['source_root'] = str(repo)
            isolated.setdefault('config', {}).setdefault('evolution', {})['enabled'] = False
            isolated['config'].setdefault('runtime', {})['execution_backend'] = 'docker'
            # Every condition receives its own frozen DB and content store. A
            # runner cannot make the candidate inherit baseline writes (or vice versa).
            source_data = Path(isolated['source_data_dir']).resolve()
            isolated_data = Path(temp) / 'data'
            isolated_data.mkdir()
            _backup_sqlite(source_data / 'evocode.db', isolated_data / 'evocode.db')
            for name in ('artifacts', 'skill_versions'):
                if (source_data / name).is_dir():
                    shutil.copytree(source_data / name, isolated_data / name)
            isolated['source_data_dir'] = str(isolated_data)
            start = time.monotonic()
            try:
                collection = isolated.get('frozen_memory_collection')
                if collection is not None and (not isinstance(collection, str) or not collection.strip()):
                    raise ValueError('Invalid frozen memory collection')
                kwargs = {'frozen_memory_collection': collection} if collection is not None else {}
                result = await asyncio.wait_for(self.runner(copy.deepcopy(spec), isolated, str(workspace), **kwargs), timeout=self.config.evolution.timeout)
                if not isinstance(result, dict) or not isinstance(result.get('passed'), bool):
                    raise ValueError('Runner must return an explicit boolean passed value')
                result = dict(result)
                if result.get('acceptance_protected') is not True:
                    result['passed'] = False
                    result['unverified'] = True
                    result['error'] = 'Runner did not attest independent protected acceptance'
                result.setdefault('duration_seconds', time.monotonic() - start)
                result['commit'] = commit
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return {'passed': False, 'error': str(exc), 'duration_seconds': time.monotonic() - start, 'unverified': True}

    async def compare(self, run_id, candidate_version_id, task_specs, baseline_snapshot, candidate_snapshot):
        if baseline_snapshot.get('frozen_memory_collection') != candidate_snapshot.get('frozen_memory_collection'):
            raise ValueError('Paired evaluation requires the same frozen memory collection')
        with tempfile.TemporaryDirectory(prefix='evocode-frozen-') as frozen:
            target = Path(frozen)
            # Use a separate read connection: backing up inside BEGIN IMMEDIATE deadlocks.
            _backup_sqlite(Path(self.store.db_path), target / 'evocode.db')
            source_dir = Path(self.config.data_dir).resolve()
            for name in ('artifacts', 'skill_versions'):
                if (source_dir / name).exists():
                    shutil.copytree(source_dir / name, target / name)
            baseline_snapshot, candidate_snapshot = copy.deepcopy(baseline_snapshot), copy.deepcopy(candidate_snapshot)
            for snapshot in (baseline_snapshot, candidate_snapshot):
                snapshot['source_data_dir'] = str(target)
                snapshot['frozen_source_root'] = str(source_dir)
            return await self._compare_frozen(run_id, candidate_version_id, task_specs, baseline_snapshot, candidate_snapshot)

    async def _compare_frozen(self, run_id, candidate_version_id, task_specs, baseline_snapshot, candidate_snapshot):
        rows = []
        for spec in task_specs:
            # Resolve HEAD once before either branch; branch-local runtime must never write source.
            frozen_spec = copy.deepcopy(spec)
            if not frozen_spec.get('commit'):
                try:
                    frozen_spec['commit'] = subprocess.check_output(['git', '-C', frozen_spec['repo_path'], 'rev-parse', 'HEAD'], text=True, stderr=subprocess.PIPE).strip()
                except (KeyError, subprocess.SubprocessError, OSError):
                    frozen_spec['commit'] = 'HEAD'
            baseline = await self._execute(frozen_spec, baseline_snapshot)
            candidate = await self._execute(frozen_spec, candidate_snapshot)
            public_spec = copy.deepcopy(frozen_spec)
            public_spec.pop('protected_acceptance_paths', None)
            rows.append({'spec': public_spec, 'baseline': baseline, 'candidate': candidate})
        metric = self.config.evolution.target_metric
        baseline_passes = sum(r['baseline'].get('passed') is True for r in rows)
        candidate_passes = sum(r['candidate'].get('passed') is True for r in rows)
        valid = bool(rows) and all(not r[side].get('unverified') for r in rows for side in ('baseline', 'candidate'))
        no_regression = all(not r['baseline'].get('passed') or r['candidate'].get('passed') for r in rows)
        def valid_metric(value):
            return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
        is_meta = self._is_meta_candidate(candidate_version_id)
        meta_comparison = None
        score_rows = rows
        if is_meta:
            meta_comparison = self._meta_injection_comparison(
                rows, task_specs, baseline_snapshot, candidate_snapshot, metric,
            )
            score_rows = [rows[index] for index in meta_comparison['scored_row_indices']]
        else:
            project_scope = self._candidate_project_scope(candidate_version_id)
            if project_scope:
                scoped_rows = [r for r in rows if r['spec'].get('repository_id') == project_scope]
                if scoped_rows:
                    score_rows = scoped_rows
            non_regression_rows = [
                r for r in score_rows
                if r['spec'].get('gate_kind') != 'regression'
                and '_replay_' not in str(r['spec'].get('task_id') or r['spec'].get('id') or '')
            ]
            if non_regression_rows:
                score_rows = non_regression_rows
        comparable = bool(score_rows) and all(valid_metric(r[s].get(metric)) for r in score_rows for s in ('baseline', 'candidate'))
        bcost = sum(r['baseline'][metric] for r in score_rows) if comparable else None
        ccost = sum(r['candidate'][metric] for r in score_rows) if comparable else None
        if is_meta:
            base_scored_passes = sum(rows[i]['baseline'].get('passed') is True for i in meta_comparison['scored_row_indices'])
            candidate_scored_passes = sum(rows[i]['candidate'].get('passed') is True for i in meta_comparison['scored_row_indices'])
            improved = candidate_scored_passes > base_scored_passes or (
                candidate_scored_passes == len(score_rows) and comparable and ccost < bcost
            )
        else:
            base_scored_passes = sum(r['baseline'].get('passed') is True for r in score_rows)
            candidate_scored_passes = sum(r['candidate'].get('passed') is True for r in score_rows)
            improved = candidate_scored_passes > base_scored_passes or (
                candidate_scored_passes == len(score_rows) and comparable and ccost < bcost
            )
        passed = bool(valid and no_regression and candidate_passes == len(rows) and improved)
        reasons = []
        if not valid: reasons.append('missing_or_unverified_acceptance')
        if not no_regression: reasons.append('acceptance_regression')
        if candidate_passes != len(rows): reasons.append('candidate_acceptance_failed')
        if not improved: reasons.append('no_measured_improvement')
        if is_meta:
            if not meta_comparison['has_comparable_pair']:
                passed = False
                reasons.append('no_comparable_meta_skill_injection_pair')
            if not meta_comparison['evidence_complete']:
                passed = False
                reasons.append('meta_skill_injection_evidence_unverified')
        report = {'evaluation_id': str(uuid.uuid4()), 'run_id': run_id, 'candidate_version_id': candidate_version_id,
                  'passed': passed, 'status': 'approved' if passed else 'needs_review', 'reasons': reasons,
                  'baseline': {'passed': baseline_passes, metric: bcost}, 'candidate': {'passed': candidate_passes, metric: ccost},
                  'target_metric': metric, 'rows': rows}
        if is_meta:
            report['meta_comparison'] = meta_comparison
        self.persist(report)
        return report

    def persist(self, report):
        with self.store.transaction() as conn:
            conn.execute('INSERT INTO evaluations(evaluation_id,run_id,payload_json,created_at) VALUES(?,?,?,?)',
                         (report['evaluation_id'], report['run_id'], json.dumps(report, ensure_ascii=False), utcnow()))
