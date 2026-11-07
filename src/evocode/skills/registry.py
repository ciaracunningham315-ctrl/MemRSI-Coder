from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


_TRIGGER_STOP_TOKENS = frozenset({
    'a', 'an', 'and', 'api', 'argument', 'arguments', 'by', 'call', 'callable',
    'calls', 'check', 'checks', 'code', 'compatibility', 'compatible', 'extension',
    'field', 'fields', 'fix', 'for', 'from', 'function', 'functions', 'in', 'into',
    'is', 'method', 'of', 'on', 'or', 'parameter', 'parameters', 'python',
    'rejection', 'rule', 'rules', 'scalar', 'signature', 'strict', 'the', 'to',
    'validation', 'validator', 'value', 'values', 'vs', 'with',
})


def now():
    return datetime.now(timezone.utc).isoformat()


class SkillRegistry:
    def __init__(self, store, data_dir):
        self.store = store
        self.root = Path(data_dir).resolve() / 'skill_versions'
        self.root.mkdir(parents=True, exist_ok=True)

    def list_skills(self):
        with self.store.transaction() as db:
            return [dict(row) for row in db.execute('SELECT * FROM skills ORDER BY name,skill_id')]

    def list_versions(self, skill_id=None):
        """List persisted skill versions, optionally narrowed to one skill.

        Metadata is decoded to the same public shape returned by ``load`` so UI
        callers do not need to understand the storage representation.
        """
        with self.store.transaction() as db:
            if skill_id is None:
                rows = db.execute(
                    'SELECT v.*,s.name,s.level,s.scope_id FROM skill_versions v '
                    'JOIN skills s USING(skill_id) ORDER BY v.created_at DESC,v.version_id'
                )
            else:
                rows = db.execute(
                    'SELECT v.*,s.name,s.level,s.scope_id FROM skill_versions v '
                    'JOIN skills s USING(skill_id) WHERE v.skill_id=? '
                    'ORDER BY v.created_at DESC,v.version_id',
                    (skill_id,),
                )
            versions = [dict(row) for row in rows]
        for version in versions:
            version['metadata'] = json.loads(version.pop('metadata_json'))
        return versions

    def snapshot(self, repository_id):
        return {s['skill_id']: s['active_version_id'] for s in self.list_skills()
                if s['status'] == 'active' and s['active_version_id'] and
                (s['level'] == 'general' or (s['level'] == 'project' and s['scope_id'] == repository_id))}

    @staticmethod
    def _tokens(value):
        return re.findall(r"\w+", str(value).lower())

    @classmethod
    def _cjk_term_matches(cls, term_tokens, tokens, token_set):
        """处理包含中文（CJK）字符的触发词子串与双字词共现匹配。"""
        if not any(re.search(r"[\u4e00-\u9fff]", tok) for tok in term_tokens):
            return False
        matched_any = False
        for tok in term_tokens:
            cjk_parts = re.findall(r"[\u4e00-\u9fff]+", tok)
            ascii_parts = [p for p in re.findall(r"[a-z0-9_]+", tok)
                           if p not in _TRIGGER_STOP_TOKENS and len(p) >= 2]
            for ascii_tok in ascii_parts:
                if ascii_tok not in token_set and not any(ascii_tok in ctx for ctx in tokens):
                    return False
                matched_any = True
            for part in cjk_parts:
                if len(part) < 2:
                    continue
                if any(part in ctx for ctx in tokens):
                    matched_any = True
                elif len(part) >= 4 and any(part[:2] in ctx for ctx in tokens) and any(part[-2:] in ctx for ctx in tokens):
                    matched_any = True
                else:
                    return False
        return matched_any

    @classmethod
    def _term_matches_tokens(cls, term_tokens, tokens, token_set):
        """判断触发词序列是否匹配目标上下文：支持连续序列、英文核心词共现及中文子串匹配。"""
        width = len(term_tokens)
        if not width:
            return False
        if any(tokens[index:index + width] == term_tokens
               for index in range(len(tokens) - width + 1)):
            return True
        if cls._cjk_term_matches(term_tokens, tokens, token_set):
            return True
        if width < 2:
            return False
        core_tokens = [tok for tok in term_tokens
                       if tok not in _TRIGGER_STOP_TOKENS and len(tok) >= 3]
        if not core_tokens:
            return False
        return all(tok in token_set for tok in core_tokens)

    def select_with_reasons(self, task_snapshot):
        """Return pinned skill candidates and an explanation for every candidate.

        Trigger terms are matched as normalized word sequences against each
        available task context, with core-token co-occurrence fallback for
        multi-word domain phrases.
        """
        cfg = task_snapshot.get('config', {}).get('skills', {})
        if not cfg.get('enabled', True):
            return {'selected': [], 'decisions': []}
        pinned = task_snapshot.get('state', {}).get('skill_snapshot')
        if pinned is None:
            pinned = self.snapshot(task_snapshot.get('repository_id'))
        state = task_snapshot.get('state', {})
        contexts = {
            'prompt': task_snapshot.get('prompt', ''),
            'effective_prompt': state.get('effective_prompt', ''),
            'user_addenda': state.get('user_addenda', []),
            'plan': state.get('plan', {}),
            'assignment': state.get('assignment', {}),
            'requirements': state.get('current_requirements', {}),
            'execution': state.get('coder_messages', []),
        }
        context_tokens = {key: self._tokens(json.dumps(value, ensure_ascii=False)
                                             if not isinstance(value, str) else value)
                          for key, value in contexts.items() if value}
        context_token_sets = {key: set(tokens) for key, tokens in context_tokens.items()}
        selected = []
        decisions = []
        for skill_id, version_id in pinned.items():
            try:
                version = self.load(version_id)
            except KeyError:
                decisions.append({'skill_id': skill_id, 'version_id': version_id,
                                  'selected': False, 'reason': 'pinned_version_missing'})
                continue
            if version['skill_id'] != skill_id:
                decisions.append({'skill_id': skill_id, 'version_id': version_id,
                                  'selected': False, 'reason': 'pinned_skill_mismatch'})
                continue
            if version['level'] == 'meta':
                decisions.append({'skill_id': skill_id, 'version_id': version_id,
                                  'selected': False, 'reason': 'meta_skill_excluded'})
                continue
            if version['level'] == 'project' and version['scope_id'] != task_snapshot.get('repository_id'):
                decisions.append({'skill_id': skill_id, 'version_id': version_id,
                                  'selected': False, 'reason': 'scope_mismatch'})
                continue
            metadata = version['metadata']
            terms = metadata.get('trigger_terms', []) or re.findall(r'\w+', version['name'].lower())
            matches = []
            for term in terms:
                term_tokens = self._tokens(term)
                if not term_tokens:
                    continue
                for context_name, tokens in context_tokens.items():
                    if self._term_matches_tokens(term_tokens, tokens, context_token_sets[context_name]):
                        matches.append({'term': str(term), 'context': context_name})
                        break
            always_load = bool(metadata.get('always_load', False))
            score = len(matches)
            chosen = bool(score or always_load)
            reason = 'trigger_match' if score else ('always_load' if always_load else 'no_trigger_match')
            decisions.append({'skill_id': skill_id, 'version_id': version_id,
                              'selected': chosen, 'reason': reason,
                              'score': score, 'matches': matches})
            if chosen:
                selected.append({**version, 'score': score, 'selection_reason': reason,
                                 'matched_triggers': matches})
        selected.sort(key=lambda v: (-v['score'], v['skill_id']))
        limit = max(0, int(cfg.get('max_loaded_skills', 3)))
        selected_ids = {item['skill_id'] for item in selected[:limit]}
        for decision in decisions:
            if decision.get('selected') and decision['skill_id'] not in selected_ids:
                decision['selected'] = False
                decision['reason'] = 'max_loaded_skills'
        return {'selected': selected[:limit], 'decisions': decisions}

    def select(self, task_snapshot):
        """Backward-compatible list-only selection API."""
        return self.select_with_reasons(task_snapshot)['selected']

    def load(self, version_id):
        with self.store.transaction() as db:
            row = db.execute('SELECT v.*,s.name,s.level,s.scope_id FROM skill_versions v JOIN skills s USING(skill_id) WHERE version_id=?', (version_id,)).fetchone()
        if row is None:
            raise KeyError(version_id)
        result = dict(row)
        result['metadata'] = json.loads(result.pop('metadata_json'))
        path = Path(result['file_path']).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('Skill content outside version store')
        content = path.read_text()
        digest = hashlib.sha256(content.encode()).hexdigest()
        if digest != result['content_sha256'] or content != result['content']:
            raise ValueError('Skill content integrity check failed')
        return result

    def create_candidate(self, name, content, level='project', scope_id=None, skill_id=None, parent_version_id=None, metadata=None):
        if level not in ('project', 'general', 'meta') or not name.strip() or not content.strip():
            raise ValueError('Invalid skill level, name or content')
        if level == 'project' and not scope_id:
            raise ValueError('Project skill requires scope_id')
        version_id = str(uuid4())
        skill_id = skill_id or str(uuid4())
        digest = hashlib.sha256(content.encode()).hexdigest()
        directory = self.root / version_id
        directory.mkdir()
        path = directory / 'SKILL.md'
        temporary = directory / '.content.tmp'
        temporary.write_text(content)
        os.replace(temporary, path)
        try:
            with self.store.transaction() as db:
                existing = db.execute('SELECT * FROM skills WHERE skill_id=?', (skill_id,)).fetchone()
                if existing:
                    if existing['level'] != level or existing['scope_id'] != scope_id:
                        raise ValueError('Skill scope is immutable; create a separate skill for generalization')
                    if parent_version_id is None:
                        parent_version_id = existing['active_version_id']
                else:
                    db.execute('INSERT INTO skills(skill_id,name,level,scope_id) VALUES (?,?,?,?)', (skill_id, name, level, scope_id))
                if parent_version_id:
                    parent = db.execute('SELECT skill_id FROM skill_versions WHERE version_id=?', (parent_version_id,)).fetchone()
                    if parent is None or parent['skill_id'] != skill_id:
                        raise ValueError('Parent belongs to a different skill')
                db.execute('INSERT INTO skill_versions VALUES (?,?,?,?,?,?,?,?,?)', (version_id, skill_id, parent_version_id, content, digest, str(path), 'draft', json.dumps(metadata or {}), now()))
        except Exception:
            path.unlink(missing_ok=True)
            directory.rmdir()
            raise
        return self.load(version_id)

    @staticmethod
    def _approved_evaluation(db, evaluation_id, version_id):
        row = db.execute('SELECT payload_json FROM evaluations WHERE evaluation_id=?', (evaluation_id,)).fetchone()
        if row is None:
            raise ValueError('Trusted persisted evaluation required')
        report = json.loads(row['payload_json'])
        target = report.get('candidate_version_id') or report.get('metadata', {}).get('candidate_version_id')
        if (report.get('evaluation_id') != evaluation_id or target != version_id or
                report.get('passed') is not True or report.get('status') not in ('passed', 'approved')):
            raise ValueError('Evaluation does not approve this candidate')
        return report

    def publish_version(self, version_id, evaluation, expected_parent_version_id=None,
                        operation=None, expected_source_versions=None):
        candidate = self.load(version_id)
        expected_source_versions = dict(expected_source_versions or {})
        with self.store.transaction() as db:
            report = self._approved_evaluation(db, evaluation.get('evaluation_id'), version_id)
            skill = db.execute('SELECT active_version_id,status,level FROM skills WHERE skill_id=?',
                               (candidate['skill_id'],)).fetchone()
            if skill is None:
                raise ValueError('Candidate skill no longer exists')
            current = skill['active_version_id']
            version_status = db.execute('SELECT status FROM skill_versions WHERE version_id=?', (version_id,)).fetchone()[0]
            parent = candidate['parent_version_id']
            if expected_parent_version_id is not None and expected_parent_version_id != parent:
                raise ValueError('Expected parent does not match candidate parent')

            if operation is None and (report.get('promotion') is not None or expected_source_versions):
                raise ValueError('Publication must apply the trusted evaluation transition')
            if operation is not None:
                if operation not in ('CREATE', 'REVISE', 'MERGE', 'GENERALIZE', 'DEPRECATE'):
                    raise ValueError('Unsupported publication operation')
                promotion = report.get('promotion')
                expected_promotion = {
                    'operation': operation,
                    'parent_version_id': parent,
                    'source_skill_versions': expected_source_versions,
                }
                if promotion != expected_promotion:
                    raise ValueError('Trusted evaluation does not authorize this publication transition')
                if skill['level'] == 'meta' and operation in ('MERGE', 'DEPRECATE', 'GENERALIZE'):
                    raise ValueError('Meta skills cannot merge, deprecate, or generalize through publication')
                if operation == 'MERGE':
                    if len(expected_source_versions) < 2:
                        raise ValueError('Merge publication requires at least two frozen source versions')
                    if candidate['skill_id'] in expected_source_versions:
                        raise ValueError('Merge candidate cannot also be a source skill')
                elif expected_source_versions:
                    raise ValueError('Only merge publication accepts source skill versions')

            expected = parent
            if current != expected:
                if current == version_id and version_status == 'active':
                    desired_skill_status = 'deprecated' if operation == 'DEPRECATE' else 'active'
                    if skill['status'] != desired_skill_status:
                        raise ValueError('Published candidate has an unexpected skill status')
                    for source_id, source_version_id in expected_source_versions.items():
                        source = db.execute('SELECT active_version_id,status FROM skills WHERE skill_id=?',
                                            (source_id,)).fetchone()
                        if source is None or source['active_version_id'] != source_version_id or source['status'] != 'deprecated':
                            raise ValueError('Merge source state changed since publication')
                    return {**candidate, 'status': version_status}
                raise ValueError('Active version changed since candidate creation')
            if version_status != 'draft':
                raise ValueError('Only draft candidates can be published')
            if current and skill['status'] != 'active':
                raise ValueError('Only active skills can be revised or deprecated')

            sources = {}
            for source_id, source_version_id in expected_source_versions.items():
                source = db.execute('SELECT active_version_id,status FROM skills WHERE skill_id=?',
                                    (source_id,)).fetchone()
                if (source is None or source['status'] != 'active' or
                        source['active_version_id'] != source_version_id):
                    raise ValueError(f'Merge source active version changed: {source_id}')
                sources[source_id] = source_version_id

            if current:
                db.execute("UPDATE skill_versions SET status='superseded' WHERE version_id=?", (current,))
            db.execute("UPDATE skill_versions SET status='active' WHERE version_id=?", (version_id,))
            new_skill_status = 'deprecated' if operation == 'DEPRECATE' else 'active'
            db.execute("UPDATE skills SET active_version_id=?,status=? WHERE skill_id=?",
                       (version_id, new_skill_status, candidate['skill_id']))
            for source_id, source_version_id in sources.items():
                changed = db.execute("UPDATE skills SET status='deprecated' "
                                     "WHERE skill_id=? AND active_version_id=? AND status='active'",
                                     (source_id, source_version_id)).rowcount
                if changed != 1:
                    raise ValueError(f'Merge source active version changed: {source_id}')
        return self.load(version_id)

    def rollback_merge(self, version_id, evaluation_id):
        """Restore the exact pre-merge state while every affected skill still matches."""
        with self.store.transaction() as db:
            candidate = self.load(version_id)
            report = self._approved_evaluation(db, evaluation_id, version_id)
            promotion = report.get('promotion') or {}
            parent_id = candidate['parent_version_id']
            sources = promotion.get('source_skill_versions') or {}
            target_id = candidate['skill_id']
            if (promotion.get('operation') != 'MERGE' or promotion.get('parent_version_id') != parent_id
                    or not isinstance(sources, dict) or len(sources) < 2 or target_id in sources):
                raise ValueError('Trusted evaluation does not describe this merge')

            target = db.execute('SELECT active_version_id,status FROM skills WHERE skill_id=?', (target_id,)).fetchone()
            if tuple(target) != (version_id, 'active') or candidate['status'] != 'active':
                raise ValueError('Merge target changed since publication')
            for source_id, source_version_id in sources.items():
                source = db.execute('SELECT active_version_id,status FROM skills WHERE skill_id=?', (source_id,)).fetchone()
                if source is None or tuple(source) != (source_version_id, 'deprecated'):
                    raise ValueError(f'Merge source changed since publication: {source_id}')
                version = self.load(source_version_id)
                if version['skill_id'] != source_id or version['status'] != 'active':
                    raise ValueError(f'Merge source version changed since publication: {source_id}')
            if parent_id:
                parent = self.load(parent_id)
                if parent['skill_id'] != target_id or parent['status'] != 'superseded':
                    raise ValueError('Merge target parent changed since publication')
                db.execute("UPDATE skill_versions SET status='active' WHERE version_id=?", (parent_id,))
            db.execute("UPDATE skill_versions SET status='superseded' WHERE version_id=?", (version_id,))
            db.execute('UPDATE skills SET active_version_id=?,status=? WHERE skill_id=?',
                       (parent_id, 'active' if parent_id else 'deprecated', target_id))
            db.executemany("UPDATE skills SET status='active' WHERE skill_id=?", ((source_id,) for source_id in sources))
            return {'target_skill_id': target_id, 'reverted_version_id': version_id,
                    'restored_target_version_id': parent_id, 'restored_source_versions': sources}

    def rollback(self, skill_id, version_id):
        target = self.load(version_id)
        if target['skill_id'] != skill_id or target['status'] not in ('active', 'superseded'):
            raise ValueError('Rollback requires a previously published version of this skill')
        with self.store.transaction() as db:
            db.execute("UPDATE skill_versions SET status='superseded' WHERE skill_id=? AND status='active'", (skill_id,))
            db.execute("UPDATE skill_versions SET status='active' WHERE version_id=?", (version_id,))
            db.execute("UPDATE skills SET active_version_id=?,status='active' WHERE skill_id=?", (version_id, skill_id))
        return self.load(version_id)

    def deprecate(self, skill_id):
        with self.store.transaction() as db:
            if not db.execute('SELECT 1 FROM skills WHERE skill_id=?', (skill_id,)).fetchone():
                raise KeyError(skill_id)
            db.execute("UPDATE skills SET status='deprecated' WHERE skill_id=?", (skill_id,))
        return next(s for s in self.list_skills() if s['skill_id'] == skill_id)
