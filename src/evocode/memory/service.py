from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL

import httpx

from evocode.config import MemoryConfig
from evocode.contracts import EvidenceRef, RetrievalResult
from evocode.redaction import redact_text


class MemoryService:
    def __init__(self, store, config):
        self.store = store
        self.config = config
        self._index_lock = asyncio.Lock()

    def _config(self, task=None):
        data = (task or {}).get('config', {}).get('raw_rag')
        return MemoryConfig.model_validate(data) if data else self.config.raw_rag

    async def _embed(self, texts, cfg):
        if not cfg.embedding_model:
            raise RuntimeError('Embedding model is not configured')
        key = os.getenv(cfg.embedding_api_key_env)
        headers = {'Authorization': f'Bearer {key}'} if key else {}
        payload = {'model': cfg.embedding_model, 'input': texts, 'encoding_format': 'float'}
        dimensions = getattr(cfg, 'embedding_dimensions', None)
        if dimensions is not None:
            payload['dimensions'] = dimensions
        last_exc = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                    response = await client.post(cfg.embedding_base_url.rstrip('/') + '/embeddings', headers=headers, json=payload)
                    response.raise_for_status()
                    rows = sorted(response.json()['data'], key=lambda x: x['index'])
                vectors = [row['embedding'] for row in rows]
                if len(vectors) != len(texts) or not vectors or not vectors[0]:
                    raise ValueError('Invalid embedding response')
                return vectors
            except Exception as exc:
                last_exc = exc
                if attempt + 1 < 3:
                    await asyncio.sleep(1.0 * (2 ** attempt))
        raise last_exc

    async def _upsert(self, chunk, cfg):
        vector = (await self._embed([chunk['text']], cfg))[0]
        base = cfg.qdrant_url.rstrip('/') + '/collections/' + cfg.collection
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.get(base)
            if response.status_code == 404:
                created = await client.put(base, json={'vectors': {'size': len(vector), 'distance': 'Cosine'}})
                if created.status_code != 409:
                    created.raise_for_status()
            else:
                response.raise_for_status()
            response = await client.put(base + '/points', params={'wait': 'true'}, json={'points': [{'id': chunk['chunk_id'], 'vector': vector, 'payload': {'repository_id': chunk['repository_id'], 'task_id': chunk['task_id']}}]})
            response.raise_for_status()

    def _make_chunks(self, event):
        payload = json.loads(event['payload_json'])
        text = json.dumps(payload, ensure_ascii=False)
        artifact_ids = [value for key, value in payload.items() if key.endswith('artifact_id') and isinstance(value, str)]
        for artifact_id in artifact_ids:
            artifact = self.store.get_artifact(artifact_id)
            path = artifact.get('storage_path') or artifact.get('path')
            if path:
                text += '\n' + Path(path).read_text(errors='replace')
        # Redact credentials before indexing while preserving the authoritative original.
        text = redact_text(text)
        event_row = {**event, 'payload': payload}
        metadata = self._evidence_metadata([event_row], text)
        for start in range(0, max(1, len(text)), 896):
            part = text[start:start + 1024]
            yield {'chunk_id': str(uuid5(NAMESPACE_URL, event['event_id'] + ':' + str(start))), 'task_id': event['task_id'], 'session_id': event['session_id'], 'repository_id': event['repository_id'], 'text': part, 'metadata': {**metadata, 'event_id': event['event_id'], 'event_ids': [event['event_id']], 'seq': event['seq'], 'seq_start': event['seq'], 'seq_end': event['seq'], 'event_type': event['event_type'], 'event_types': [event['event_type']], 'chunk_type': 'event', 'offset': start}}

        # Keep a bounded causal episode beside raw event windows. Stable IDs make
        # repeated jobs/rebuilds overwrite the same evidence instead of multiplying it.
        if event['event_type'] in {'test.result', 'verification.result', 'task.failed', 'assistant.final'}:
            episode = self._make_episode(event_row)
            if episode:
                yield from episode

    def _make_episode(self, anchor):
        events = self.store.read_events(task_id=anchor['task_id'])
        anchor_index = next((i for i, event in enumerate(events) if event['event_id'] == anchor['event_id']), None)
        if anchor_index is None:
            return
        start_index = self._episode_failure_start(events, anchor_index)
        if start_index is None:
            return
        relevant = []
        char_count = 0
        terminal_types = {'task.completed', 'task.failed', 'task.error', 'assistant.final'}
        for item in events[start_index:]:
            event_type = item['event_type']
            if not event_type.startswith(('tool.', 'test.', 'code.', 'task.error', 'task.failed', 'task.completed', 'verification.', 'assistant.final')):
                continue
            encoded = json.dumps(item.get('payload', {}), ensure_ascii=False)
            if relevant and (len(relevant) >= 100 or char_count + len(encoded) > 65536):
                break
            relevant.append(item)
            char_count += len(encoded)
            if event_type in terminal_types or self._is_success_boundary(item):
                break
        # Index only finalized or deterministically capped episodes so async indexing cannot leave partial duplicates.
        truncated = bool(relevant and (len(relevant) >= 100 or char_count >= 65536))
        if not relevant or (not truncated and relevant[-1]['event_type'] not in terminal_types and not self._is_success_boundary(relevant[-1])):
            return
        rows = []
        artifact_text = []
        for item in relevant:
            payload = item.get('payload') or {}
            rows.append({**item, 'payload': payload})
            for key, value in payload.items():
                if key.endswith('artifact_id') and isinstance(value, str):
                    artifact = self.store.get_artifact(value)
                    path = artifact.get('storage_path') or artifact.get('path')
                    if path:
                        try:
                            artifact_text.append(Path(path).read_text(errors='replace'))
                        except OSError:
                            pass
        source = '\n'.join(e['event_type'] + ': ' + json.dumps(e.get('payload', {}), ensure_ascii=False) for e in relevant)
        source += '\n' + '\n'.join(artifact_text)
        source = redact_text(source)[:65536]
        metadata = self._evidence_metadata(rows, source)
        ids = [e['event_id'] for e in relevant]
        low, high = int(relevant[0]['seq']), int(relevant[-1]['seq'])
        chunk_base = str(uuid5(NAMESPACE_URL, 'episode:' + ':'.join(ids)))
        for start in range(0, max(1, len(source)), 896):
            yield {
                'chunk_id': str(uuid5(NAMESPACE_URL, chunk_base + ':' + str(start))),
                'task_id': anchor['task_id'], 'session_id': anchor['session_id'],
                'repository_id': anchor['repository_id'], 'text': source[start:start + 1024],
                'metadata': {**metadata, 'event_id': anchor['event_id'], 'event_ids': ids,
                    'seq': anchor['seq'], 'seq_start': low, 'seq_end': high,
                    'event_type': anchor['event_type'], 'event_types': [e['event_type'] for e in relevant],
                    'event_count': len(ids), 'truncated': truncated, 'chunk_type': 'episode', 'offset': start},
            }

    @staticmethod
    def _is_success_boundary(event):
        if event['event_type'] not in ('test.result', 'verification.result'):
            return False
        payload = event.get('payload') or {}
        return payload.get('passed') is True or payload.get('status') in ('passed', 'completed', 'success')

    def _episode_failure_start(self, events, anchor_index):
        anchor = events[anchor_index]
        anchor_type = anchor['event_type']
        failure_types = {'tool.failed', 'task.error', 'task.failed'}
        if anchor_type == 'tool.failed':
            return anchor_index
        payload = anchor.get('payload') or {}
        if anchor_type in ('test.result', 'verification.result') and (payload.get('passed') is False or payload.get('status') in ('failed', 'error')):
            return anchor_index
        if anchor_type not in {'test.result', 'verification.result', 'assistant.final', 'task.failed', 'task.error'}:
            return None
        failure_index = None
        for index in range(anchor_index - 1, -1, -1):
            item = events[index]
            event_type = item['event_type']
            payload = item.get('payload') or {}
            if event_type in failure_types:
                failure_index = index
            if event_type in ('test.result', 'verification.result') and (payload.get('passed') is False or payload.get('status') in ('failed', 'error')):
                failure_index = index
            if self._is_success_boundary(item):
                return failure_index
            if event_type in ('task.completed', 'task.failed', 'task.error'):
                return failure_index
        if failure_index is not None:
            return failure_index
        return anchor_index if anchor_type in ('task.failed', 'task.error') else None

    def _evidence_metadata(self, events, text):
        paths, symbols, dependencies = set(), set(), set()
        source_commit = None
        content_evidence = {}
        dependency_evidence = {}
        for event in events:
            payload = event.get('payload') or {}
            commit = payload.get('git_commit')
            if isinstance(commit, str) and commit:
                source_commit = commit
            nested = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}
            for source in (payload, nested):
                for item in source.get('content_evidence', []) or []:
                    if isinstance(item, dict) and isinstance(item.get('path'), str) and isinstance(item.get('sha256'), str):
                        content_evidence[item['path']] = {key: item[key] for key in ('path', 'sha256', 'source') if key in item}
                for item in source.get('dependency_evidence', []) or []:
                    if isinstance(item, dict) and isinstance(item.get('path'), str) and isinstance(item.get('sha256'), str):
                        dependency_evidence[item['path']] = {key: item[key] for key in ('path', 'sha256', 'source') if key in item}
            candidates = payload.get('file_paths') or []
            if isinstance(candidates, str):
                candidates = [candidates]
            if isinstance(candidates, list):
                paths.update(p for p in candidates if isinstance(p, str) and p)
            for key in ('file_path', 'path', 'filename'):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    paths.add(value)
            for source in (payload, nested):
                for key in ('symbol', 'symbols', 'function', 'function_name', 'class', 'class_name'):
                    value = source.get(key)
                    if isinstance(value, str) and value:
                        symbols.add(value)
                    elif isinstance(value, list):
                        symbols.update(v for v in value if isinstance(v, str) and v)
                for key in ('dependency', 'dependencies', 'package', 'packages', 'framework', 'version'):
                    value = source.get(key)
                    if isinstance(value, (str, int, float)) and value:
                        dependencies.add(f'{key}:{value}')
                    elif isinstance(value, list):
                        dependencies.update(f'{key}:{v}' for v in value if isinstance(v, (str, int, float)))
        # Recover paths and declared Python symbols from unified patch text.
        paths.update(re.findall(r'^(?:\+\+\+|---) [ab]/([^\s]+)', text, re.M))
        symbols.update(re.findall(r'^\+\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)', text, re.M))
        unique_paths = sorted(paths)
        return {
            'git_commit': source_commit,
            'file_paths': unique_paths, 'symbols': sorted(symbols),
            'dependencies': sorted(dependencies), 'content_evidence': list(content_evidence.values()),
            'dependency_evidence': list(dependency_evidence.values()),
        }

    async def index_pending(self, limit=100, *, allowed_event_ids=None):
        cfg = self.config.raw_rag
        if not cfg.enabled and not cfg.index_when_disabled:
            return {'status': 'disabled', 'indexed': 0}
        async with self._index_lock:
            with self.store.transaction() as db:
                query = "SELECT e.*,j.job_id FROM index_jobs j JOIN events e USING(event_id) WHERE j.status IN ('pending','failed')"
                args = []
                if allowed_event_ids is not None:
                    if not allowed_event_ids:
                        return {'status': 'ready', 'indexed': 0, 'failed': 0}
                    query += ' AND e.event_id IN (' + ','.join('?' for _ in allowed_event_ids) + ')'
                    args.extend(allowed_event_ids)
                rows = [dict(r) for r in db.execute(query + ' ORDER BY e.created_at,e.seq LIMIT ?', (*args, limit))]
            indexed = failed = deferred = 0
            for event in rows:
                try:
                    chunks = list(self._make_chunks(event))
                    if allowed_event_ids is not None:
                        allowed = set(allowed_event_ids)
                        if any(not set(chunk['metadata'].get('event_ids', [])) <= allowed for chunk in chunks):
                            # Keep the whole job pending: a later stage can retry
                            # the episode when its complete event range is allowed.
                            deferred += 1
                            continue
                    for chunk in chunks:
                        with self.store.transaction() as db:
                            db.execute('INSERT OR REPLACE INTO memory_chunks VALUES (?,?,?,?,?,?)', (chunk['chunk_id'], chunk['task_id'], chunk['session_id'], chunk['repository_id'], chunk['text'], json.dumps(chunk['metadata'])))
                            db.execute('DELETE FROM memory_fts WHERE chunk_id=?', (chunk['chunk_id'],))
                            db.execute('INSERT INTO memory_fts VALUES (?,?)', (chunk['chunk_id'], chunk['text']))
                    for chunk in chunks:
                        await self._upsert(chunk, cfg)
                    with self.store.transaction() as db:
                        db.execute("UPDATE index_jobs SET status='completed',last_error=NULL WHERE job_id=?", (event['job_id'],))
                    indexed += 1
                except Exception as exc:
                    with self.store.transaction() as db:
                        db.execute("UPDATE index_jobs SET status='failed',retries=retries+1,last_error=? WHERE job_id=?", (str(exc)[:1000], event['job_id']))
                    failed += 1
            return {'status': 'degraded' if failed or deferred else 'ready', 'indexed': indexed, 'failed': failed, 'deferred': deferred}

    def status(self):
        with self.store.transaction() as db:
            states = {r['status']: r['n'] for r in db.execute('SELECT status,count(*) n FROM index_jobs GROUP BY status')}
            chunks = db.execute('SELECT count(*) FROM memory_chunks').fetchone()[0]
        return {'enabled': self.config.raw_rag.enabled, 'jobs': states, 'chunks': chunks, 'embedding_configured': bool(self.config.raw_rag.embedding_model)}

    async def rebuild_index(self):
        with self.store.transaction() as db:
            db.execute("UPDATE index_jobs SET status='pending',last_error=NULL")
        # Stable point IDs overwrite existing vectors. Authoritative chunks are not deleted during rebuild.
        total = self.status()['jobs'].get('pending', 0)
        return await self.index_pending(max(total, 1))

    async def _dense(self, query, task, cfg):
        vector = (await self._embed([query], cfg))[0]
        must = [{'key': 'repository_id', 'match': {'value': task['repository_id']}}]
        if cfg.history_allowed_task_ids is not None:
            must.append({'key': 'task_id', 'match': {'any': cfg.history_allowed_task_ids}})
        body = {'vector': vector, 'limit': cfg.dense_top_k, 'with_payload': False, 'filter': {'must': must, 'must_not': [{'key': 'task_id', 'match': {'value': task['task_id']}}]}}
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.post(cfg.qdrant_url.rstrip('/') + '/collections/' + cfg.collection + '/points/search', json=body)
            if response.status_code == 404:
                raise RuntimeError('Qdrant collection does not exist')
            response.raise_for_status()
        return [str(row['id']) for row in response.json()['result']]

    async def _rerank(self, query, rows, cfg):
        top_k = max(0, int(getattr(cfg, 'rerank_top_k', 5)))
        candidates = rows[:top_k]
        if not candidates:
            return rows

        provider = getattr(cfg, 'rerank_provider', 'compatible')
        key_env = getattr(cfg, 'rerank_api_key_env', 'EVOCODE_RERANK_API_KEY')
        key = os.getenv(key_env)
        if provider == 'dashscope' and not key:
            raise RuntimeError(f'{key_env} is required for DashScope reranking')
        headers = {'Authorization': f'Bearer {key}'} if key else {}
        documents = [row['text'] for row in candidates]
        if provider == 'dashscope':
            payload = {
                'model': cfg.rerank_model,
                'input': {'query': query, 'documents': documents},
                'parameters': {'top_n': len(candidates)},
            }
        elif provider == 'compatible':
            payload = {'model': cfg.rerank_model, 'query': query, 'documents': documents, 'top_n': len(candidates)}
        else:
            raise ValueError(f'Unsupported rerank provider: {provider}')

        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.post(cfg.rerank_url, headers=headers, json=payload)
            response.raise_for_status()
        data = response.json()
        if provider == 'dashscope':
            results = data.get('output', {}).get('results')
        else:
            results = data.get('results')
        if not isinstance(results, list):
            raise ValueError('Invalid reranker response: results must be a list')

        ranked_indices = []
        seen = set()
        for item in results:
            if not isinstance(item, dict):
                continue
            index = item.get('index')
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(candidates) or index in seen:
                continue
            seen.add(index)
            score = item.get('relevance_score', item.get('score'))
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                score = None
            ranked_indices.append((index, score))
        if not ranked_indices:
            raise ValueError('Invalid reranker response: no valid result indices')
        if any(score is not None for _, score in ranked_indices):
            ranked_indices.sort(key=lambda pair: (pair[1] is None, -(pair[1] or 0)))
        selected = [candidates[index] for index, _ in ranked_indices]
        selected.extend(candidates[index] for index in range(len(candidates)) if index not in seen)
        return selected + rows[top_k:]

    def _applicability(self, metadata, task):
        root = Path(task.get('repo_path', '.')).resolve()
        checks = {}
        reasons = []
        repository_ok = metadata.get('repository_id') in (None, task.get('repository_id'))
        checks['repository'] = {'status': 'valid' if repository_ok else 'stale'}
        if not repository_ok:
            reasons.append('repository identity differs')

        paths = metadata.get('file_paths', [])
        if isinstance(paths, str):
            paths = [paths]
        elif not isinstance(paths, list):
            paths = []
        resolved_paths = {}
        path_status = 'valid' if paths else 'unknown'
        for name in paths:
            path = (root / name).resolve()
            if not path.is_relative_to(root) or not path.exists():
                path_status = 'stale'
                reasons.append(f'source path is missing or outside repository: {name}')
            else:
                resolved_paths[name] = path
        checks['paths'] = {'status': path_status, 'paths': list(paths)}

        expected_content = metadata.get('content_evidence') or []
        content_status = 'unknown' if not expected_content else 'valid'
        for item in expected_content:
            if not isinstance(item, dict):
                content_status = 'unknown'
                continue
            name = item.get('path')
            path = resolved_paths.get(name)
            if path is None:
                content_status = 'stale'
                reasons.append(f'content source is unavailable: {name}')
                continue
            try:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                content_status = 'unknown'
                reasons.append(f'content could not be read: {name}')
                continue
            if actual != item.get('sha256'):
                content_status = 'stale'
                reasons.append(f'source content changed: {name}')
        checks['content'] = {'status': content_status, 'paths': [x.get('path') for x in expected_content]}

        expected_dependencies = metadata.get('dependency_evidence') or []
        dependency_status = 'unknown' if not expected_dependencies else 'valid'
        for item in expected_dependencies:
            if not isinstance(item, dict):
                dependency_status = 'unknown'
                continue
            name = item.get('path')
            path = (root / name).resolve() if isinstance(name, str) else None
            if path is not None and not path.is_relative_to(root):
                path = None
            if path is None:
                dependency_status = 'stale'
                reasons.append(f'dependency manifest is unavailable: {name}')
                continue
            try:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                dependency_status = 'unknown'
                reasons.append(f'dependency manifest could not be read: {name}')
                continue
            if actual != item.get('sha256'):
                dependency_status = 'stale'
                reasons.append(f'dependency manifest changed: {name}')
        checks['dependencies'] = {'status': dependency_status, 'paths': [x.get('path') for x in expected_dependencies]}

        symbols = metadata.get('symbols') or []
        symbol_status = 'unknown' if not symbols else 'valid'
        found_symbols = set()
        if symbols:
            for name, path in resolved_paths.items():
                try:
                    source = path.read_text(errors='replace')
                except OSError:
                    continue
                if path.suffix == '.py':
                    try:
                        tree = ast.parse(source)
                        found_symbols.update(node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
                    except SyntaxError:
                        pass
                else:
                    found_symbols.update(re.findall(r'\b(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)', source))
            missing = sorted(set(symbols) - found_symbols)
            if missing:
                symbol_status = 'stale' if resolved_paths else 'unknown'
                if resolved_paths:
                    reasons.append('source symbols are missing: ' + ', '.join(missing))
        checks['symbols'] = {'status': symbol_status, 'symbols': list(symbols)}

        commit = metadata.get('git_commit')
        git_status = 'unknown'
        if commit:
            try:
                result = subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', commit, 'HEAD'], capture_output=True, timeout=5)
                if result.returncode == 0:
                    git_status = 'valid'
                elif result.returncode == 1:
                    git_status = 'stale'
                    reasons.append('source commit is not an ancestor of current HEAD')
                else:
                    reasons.append('git ancestry could not be checked')
            except (OSError, subprocess.SubprocessError):
                reasons.append('git ancestry could not be checked')
        checks['git'] = {'status': git_status, 'commit': commit}

        statuses = [check['status'] for check in checks.values()]
        if 'stale' in statuses:
            status = 'stale'
        elif all(value == 'valid' for value in statuses) and any(check['status'] == 'valid' for key, check in checks.items() if key in ('content', 'dependencies', 'symbols')):
            status = 'valid'
        else:
            status = 'unknown'
            if not reasons:
                reasons.append('insufficient current source evidence to confirm applicability')
        return {'status': status, 'checks': checks, 'reasons': reasons}

    def _validity(self, metadata, task):
        return self._applicability(metadata, task)['status']

    @staticmethod
    def _clip_utf8(text, byte_limit):
        return str(text).encode('utf-8')[:max(0, byte_limit)].decode('utf-8', errors='ignore')

    def _episode_retrieval_text(self, ranked_text, events, byte_limit):
        if byte_limit <= 0:
            return ''
        ordered = events
        failures = [event for event in ordered if event['event_type'] in ('tool.failed', 'task.error', 'task.failed') or (
            event['event_type'] in ('test.result', 'verification.result') and (
                event['payload'].get('passed') is False or event['payload'].get('status') in ('failed', 'error')
            )
        )]
        patches = [event for event in ordered if event['event_type'] == 'code.patch']
        terminals = [event for event in ordered if event['event_type'] in ('test.result', 'verification.result', 'task.completed', 'task.failed', 'task.error', 'assistant.final')]
        selected = []
        for category, candidates in (('failure', failures), ('patch', patches), ('verification/terminal', terminals)):
            if candidates:
                selected.append((category, candidates[0] if category == 'failure' else candidates[-1]))

        anchor_budget = min(1280, byte_limit // 2)
        label_bytes = sum(len(f'[{label} {event["event_type"]}] '.encode('utf-8')) for label, event in selected)
        separator_bytes = max(0, len(selected) - 1)
        per_excerpt = max(0, anchor_budget - label_bytes - separator_bytes) // max(1, len(selected))
        blocks = []
        for label, event in selected:
            payload = event['payload']
            details = {key: payload[key] for key in ('passed', 'status', 'error', 'message', 'reason', 'output', 'summary', 'patch', 'content', 'result') if key in payload}
            if label == 'patch' and not details.get('patch'):
                artifact_id = payload.get('artifact_id') or payload.get('patch_artifact_id')
                if artifact_id:
                    artifact = self.store.get_artifact(artifact_id)
                    path = artifact.get('storage_path') or artifact.get('path')
                    if path:
                        try:
                            details['patch'] = Path(path).read_text(errors='replace')
                        except OSError:
                            pass
            excerpt = redact_text(json.dumps(details or payload, ensure_ascii=False))
            blocks.append(f'[{label} {event["event_type"]}] {self._clip_utf8(excerpt, per_excerpt)}')
        anchor_text = '\n'.join(blocks)
        anchor_bytes = len(anchor_text.encode('utf-8'))
        ranked_prefix = '[ranked excerpt] '
        separator_bytes = 1 if anchor_text else 0
        ranked_budget = max(0, byte_limit - anchor_bytes - len(ranked_prefix.encode('utf-8')) - separator_bytes)
        ranked_excerpt = self._clip_utf8(redact_text(ranked_text), ranked_budget)
        if ranked_excerpt:
            return ranked_prefix + ranked_excerpt + (('\n' + anchor_text) if anchor_text else '')
        return anchor_text

    def _episode_events(self, row, metadata):
        event_ids = metadata.get('event_ids') or []
        if not event_ids:
            return []
        placeholders = ','.join('?' for _ in event_ids)
        with self.store.transaction() as db:
            rows = [dict(item) for item in db.execute(
                f'SELECT event_id,event_type,payload_json FROM events WHERE task_id=? AND event_id IN ({placeholders})',
                (row['task_id'], *event_ids),
            )]
        by_id = {item['event_id']: item for item in rows}
        events = []
        for event_id in event_ids:
            item = by_id.get(event_id)
            if item is None:
                continue
            try:
                payload = json.loads(item['payload_json'])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            events.append({**item, 'payload': payload if isinstance(payload, dict) else {}})
        return events

    @staticmethod
    def _neighbor_is_context(event_type):
        return event_type in {
            'user.message', 'planner.plan', 'planner.replanned', 'assistant.message', 'assistant.final',
            'coder.report', 'tool.started', 'tool.completed', 'tool.failed', 'tool.interrupted',
            'tool.resolved', 'test.result', 'code.patch', 'verification.result', 'task.error',
            'task.failed', 'task.completed',
        }

    async def search(self, query, task_snapshot):
        cfg = self._config(task_snapshot)
        if not cfg.enabled:
            return RetrievalResult(status='memory_disabled')
        query = redact_text(query)
        terms = re.findall(r'\w+', query, re.UNICODE)
        if not terms:
            return RetrievalResult(status='empty', mode='fts')
        match = ' OR '.join('"' + term.replace('"', '""') + '"' for term in terms[:32])
        allowed_tasks = None if cfg.history_allowed_task_ids is None else set(cfg.history_allowed_task_ids)
        allowed_events = None if cfg.history_allowed_event_ids is None else set(cfg.history_allowed_event_ids)
        if allowed_tasks is not None and not allowed_tasks:
            return RetrievalResult(status='empty', mode='fts')
        with self.store.transaction() as db:
            sql = 'SELECT f.chunk_id FROM memory_fts f JOIN memory_chunks c ON c.chunk_id=f.chunk_id WHERE memory_fts MATCH ? AND c.repository_id=? AND c.task_id != ?'
            params = [match, task_snapshot['repository_id'], task_snapshot['task_id']]
            if allowed_tasks is not None:
                sql += ' AND c.task_id IN (' + ','.join('?' for _ in allowed_tasks) + ')'
                params.extend(sorted(allowed_tasks))
            if allowed_events is not None:
                sql += " AND EXISTS (SELECT 1 FROM json_each(?) WHERE value=json_extract(c.metadata_json, '$.event_id'))"
                params.append(json.dumps(sorted(allowed_events)))
            fts = [r['chunk_id'] for r in db.execute(sql + ' ORDER BY bm25(memory_fts) LIMIT ?', (*params, cfg.dense_top_k))]
        reasons = []
        try:
            dense = await self._dense(query, task_snapshot, cfg)
            mode = 'hybrid'
        except Exception as exc:
            dense = []
            mode = 'fts'
            reasons.append('dense unavailable: ' + str(exc)[:200])
        scores = {}
        for ranking in (fts, dense):
            for rank, identifier in enumerate(ranking):
                scores[identifier] = scores.get(identifier, 0) + 1 / (60 + rank + 1)
        rows = []
        with self.store.transaction() as db:
            for identifier in sorted(scores, key=lambda key: -scores[key]):
                row = db.execute('SELECT * FROM memory_chunks WHERE chunk_id=? AND repository_id=? AND task_id != ?', (identifier, task_snapshot['repository_id'], task_snapshot['task_id'])).fetchone()
                if row and (allowed_tasks is None or row['task_id'] in allowed_tasks):
                    metadata = json.loads(row['metadata_json'])
                    if allowed_events is not None and not set(metadata.get('event_ids') or [metadata['event_id']]) <= allowed_events:
                        continue
                    rows.append({**dict(row), 'text': redact_text(row['text'])})
        reranked = False
        if cfg.rerank_enabled and cfg.rerank_top_k > 0 and rows:
            try:
                rows = await self._rerank(query, rows, cfg)
                reranked = True
            except Exception as exc:
                reasons.append('reranker unavailable: ' + str(exc)[:200])
        rows_with_metadata = [(row, json.loads(row['metadata_json'])) for row in rows]
        episodes_by_member = {}
        for row, metadata in rows_with_metadata:
            if metadata.get('chunk_type') == 'episode':
                for member in metadata.get('event_ids', []):
                    episodes_by_member.setdefault(member, (row, metadata))
        evidence = []
        used_events = set()
        # UTF-8 bytes are a conservative token upper bound across byte-based tokenizers.
        remaining = cfg.max_context_tokens
        for row, metadata in rows_with_metadata:
            # A recalled episode subsumes its plain event hits. Keep the best
            #-ranked position among them so raw chunks cannot crowd it out.
            if metadata.get('chunk_type') != 'episode':
                row, metadata = episodes_by_member.get(metadata['event_id'], (row, metadata))
            event_id = metadata['event_id']
            if event_id in used_events or remaining <= 0:
                continue
            used_events.add(event_id)
            if metadata.get('chunk_type') == 'episode':
                episode_events = self._episode_events(row, metadata)
                metadata['related_event_ids'] = [event['event_id'] for event in episode_events]
                text = self._episode_retrieval_text(row['text'], episode_events, remaining)
            else:
                # Expand only useful adjacent observations; internal state deltas,
                # context artifacts, and receipts are not retrieval evidence.
                with self.store.transaction() as db:
                    neighbors = [dict(r) for r in db.execute('SELECT event_id,event_type,payload_json FROM events WHERE task_id=? AND seq BETWEEN ? AND ? ORDER BY seq', (row['task_id'], metadata['seq'] - 1, metadata['seq'] + 1))]
                neighbors = [n for n in neighbors if n['event_id'] != event_id and self._neighbor_is_context(n['event_type']) and (allowed_events is None or n['event_id'] in allowed_events)]
                metadata['related_event_ids'] = [event_id] + [n['event_id'] for n in neighbors]
                text = row['text']
                for neighbor in neighbors:
                    text += '\n' + neighbor['event_type'] + ': ' + self._clip_utf8(redact_text(neighbor['payload_json']), 320)
                text = self._clip_utf8(text, remaining)
            remaining -= len(text.encode('utf-8'))
            applicability = await asyncio.to_thread(self._applicability, {**metadata, 'repository_id': row['repository_id']}, task_snapshot)
            metadata['applicability'] = applicability
            evidence.append(EvidenceRef(event_id=event_id, session_id=row['session_id'], task_id=row['task_id'], chunk_id=row['chunk_id'], git_commit=metadata.get('git_commit'), state_validity=applicability['status'], text=text, score=scores[row['chunk_id']], metadata=metadata))
            if len(evidence) >= cfg.final_evidence_count:
                break
        return RetrievalResult(
            status='degraded' if reasons else ('ok' if evidence else 'empty'),
            evidence=evidence, mode=mode, reason='; '.join(reasons) or None,
            fts_hits=len(fts), dense_hits=len(dense), reranked=reranked,
        )
