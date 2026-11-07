import json
import pytest
from evocode.config import AppConfig
from evocode.contracts import TaskSpec
from evocode.memory import MemoryService
from evocode.persistence import EventStore


@pytest.fixture
def setup(tmp_path):
    cfg = AppConfig(data_dir=str(tmp_path))
    store = EventStore(tmp_path / 'db.sqlite')
    task = store.create_task(TaskSpec(repo_path=str(tmp_path), prompt='fix jwt expiration'), cfg.snapshot())
    yield store, MemoryService(store, cfg), task
    store.close()


def _new_search_task(store, task):
    """Search historical evidence from a distinct task in the same repository."""
    return store.create_task(TaskSpec(repo_path=task['repo_path'], prompt='investigate current issue',
                                      repository_id=task['repository_id']), task['config'])


@pytest.mark.asyncio
async def test_disabled_never_calls_retrieval(setup, monkeypatch):
    store, memory, task = setup
    task['config']['raw_rag']['enabled'] = False
    async def forbidden(*args):
        raise AssertionError('Retrieval must not run')
    monkeypatch.setattr(memory, '_dense', forbidden)
    assert (await memory.search('jwt', task)).status == 'memory_disabled'


@pytest.mark.asyncio
async def test_fts_survives_vector_failure_and_is_idempotent(setup):
    store, memory, task = setup
    event = store.append_event(task['task_id'], 'test.result', {'output': 'JWT expired ' + 'payload ' * 500})
    first = await memory.index_pending()
    assert first['failed'] > 0
    count = memory.status()['chunks']
    assert count > 2
    await memory.index_pending()
    assert memory.status()['chunks'] == count
    result = await memory.search('JWT', _new_search_task(store, task))
    assert result.status == 'degraded' and result.mode == 'fts'
    assert any(e.event_id == event['event_id'] for e in result.evidence)
    other = dict(task, repository_id='other')
    assert (await memory.search('JWT', other)).evidence == []


@pytest.mark.asyncio
async def test_hybrid_dedupe_rebuild_and_reference(setup, monkeypatch):
    store, memory, task = setup
    search_task = _new_search_task(store, task)
    async def upsert(*args):
        return None
    monkeypatch.setattr(memory, '_upsert', upsert)
    assert (await memory.index_pending())['status'] == 'ready'
    with store.transaction() as db:
        chunk = db.execute('SELECT chunk_id FROM memory_chunks WHERE task_id=?', (task['task_id'],)).fetchone()[0]
    async def dense(*args):
        return [chunk]
    monkeypatch.setattr(memory, '_dense', dense)
    result = await memory.search('jwt', search_task)
    assert result.mode == 'hybrid'
    assert len(result.evidence) == 1
    assert result.evidence[0].metadata['related_event_ids']
    before = memory.status()['chunks']
    await memory.rebuild_index()
    assert memory.status()['chunks'] == before


@pytest.mark.asyncio
async def test_budget_and_stale_paths(setup, monkeypatch):
    store, memory, task = setup
    store.append_event(task['task_id'], 'test.result', {'output': 'expiration failure', 'file_paths': ['missing.py']})
    async def upsert(*args):
        pass
    monkeypatch.setattr(memory, '_upsert', upsert)
    await memory.index_pending()
    task['config']['raw_rag']['max_context_tokens'] = 100
    result = await memory.search('expiration', _new_search_task(store, task))
    assert sum(len(e.text.encode()) for e in result.evidence) <= 100
    with store.transaction() as db:
        import json
        row = db.execute("SELECT metadata_json FROM memory_chunks WHERE text LIKE '%missing.py%'").fetchone()
    assert memory._validity(json.loads(row[0]), task) == 'stale'


@pytest.mark.asyncio
async def test_artifact_log_middle_is_searchable_with_original_event_reference(setup, monkeypatch):
    store, memory, task = setup
    prefix = 'startup worker-ready\n' + ('routine log line\n' * 160)
    matched = 'REQUEST trace marker 7f3a9c complete traceback follows\n'
    suffix = ('shutdown log line\n' * 100)
    artifact = store.save_artifact(prefix + matched + suffix, kind='log')
    event = store.append_event(task['task_id'], 'worker.failed', {'log_artifact_id': artifact['artifact_id']})

    async def upsert(*args):
        pass

    async def unavailable(*args):
        raise RuntimeError('vector service unavailable')

    monkeypatch.setattr(memory, '_upsert', upsert)
    monkeypatch.setattr(memory, '_dense', unavailable)
    indexed = await memory.index_pending()
    assert indexed['indexed'] >= 1
    task['config']['raw_rag']['max_context_tokens'] = 8192
    result = await memory.search('7f3a9c', _new_search_task(store, task))

    assert result.mode == 'fts'
    evidence = next(item for item in result.evidence if item.event_id == event['event_id'])
    assert 'REQUEST trace marker 7f3a9c' in evidence.text
    assert 'complete traceback follows' in evidence.text
    assert evidence.metadata['event_id'] == event['event_id']


@pytest.mark.asyncio
async def test_credentials_are_redacted_in_indexes_reranking_and_neighbor_evidence(setup, monkeypatch):
    store, memory, task = setup
    source = store.append_event(task['task_id'], 'test.result', {
        'output': 'hydration trace password=synthetic-log-password',
        'api_key': 'synthetic-index-key',
    })
    neighbor = store.append_event(task['task_id'], 'tool.completed', {'secret': 'synthetic-neighbor-secret'})

    async def check_upsert(chunk, config):
        assert 'synthetic-' not in chunk['text']

    async def no_dense_match(*args):
        assert 'synthetic-' not in args[0]
        return []

    async def check_rerank(query, rows, config):
        assert 'synthetic-' not in query
        assert all('synthetic-' not in row['text'] for row in rows)
        return rows

    monkeypatch.setattr(memory, '_upsert', check_upsert)
    monkeypatch.setattr(memory, '_dense', no_dense_match)
    monkeypatch.setattr(memory, '_rerank', check_rerank)
    assert (await memory.index_pending())['status'] == 'ready'
    # An existing SQLite index can predate redaction at write time.
    with store.transaction() as db:
        db.execute(
            'UPDATE memory_chunks SET text=? WHERE json_extract(metadata_json,?)=?',
            ('hydration trace password=synthetic-legacy-password', '$.event_id', source['event_id']),
        )
    task['config']['raw_rag']['rerank_enabled'] = True
    result = await memory.search('hydration password=synthetic-query-password', _new_search_task(store, task))
    assert result.status == 'ok'
    evidence = next(item for item in result.evidence if item.event_id == source['event_id'])
    assert 'synthetic-' not in evidence.text
    assert evidence.text.count('[REDACTED]') == 2
    assert neighbor['event_id'] in evidence.metadata['related_event_ids']
    original = store.read_events(task_id=task['task_id'])
    assert any(e['payload'].get('secret') == 'synthetic-neighbor-secret' for e in original)


@pytest.mark.asyncio
async def test_all_memory_http_clients_ignore_proxy_environment(setup, monkeypatch):
    _, memory, task = setup
    calls = []

    class Response:
        status_code = 200

        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            pass

        def json(self):
            if self.url.endswith('/embeddings'):
                return {'data': [{'index': 0, 'embedding': [0.1, 0.2]}]}
            if self.url.endswith('/points/search'):
                return {'result': []}
            if self.url.endswith('/rerank'):
                return {'results': [{'index': 0}]}
            return {}

    class Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            return Response(url)

        async def put(self, url, **kwargs):
            return Response(url)

        async def post(self, url, **kwargs):
            return Response(url)

    monkeypatch.setattr('evocode.memory.service.httpx.AsyncClient', Client)
    cfg = memory.config.raw_rag.model_copy(update={'embedding_model': 'test-embedding'})
    await memory._embed(['query'], cfg)
    await memory._upsert({'text': 'chunk', 'chunk_id': 'chunk-1', 'repository_id': 'repo', 'task_id': 'task'}, cfg)
    await memory._dense('query', {'repository_id': 'repo', 'task_id': 'current-task'}, cfg)
    await memory._rerank('query', [{'text': 'chunk'}], cfg)

    assert len(calls) == 6
    assert all(call.get('trust_env') is False for call in calls)


@pytest.mark.asyncio
async def test_embedding_sends_float_format_and_optional_dimensions(setup, monkeypatch):
    _, memory, _ = setup
    requests = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {'data': [{'index': 0, 'embedding': [0.1, 0.2]}]}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return Response()

    monkeypatch.setattr('evocode.memory.service.httpx.AsyncClient', Client)
    cfg = memory.config.raw_rag.model_copy(update={
        'embedding_model': 'embedding-test',
        'embedding_base_url': 'https://offline.invalid/v1',
        'embedding_dimensions': 1024,
    })
    assert await memory._embed(['sample'], cfg) == [[0.1, 0.2]]
    url, request = requests[0]
    assert url == 'https://offline.invalid/v1/embeddings'
    assert request['json'] == {
        'model': 'embedding-test',
        'input': ['sample'],
        'encoding_format': 'float',
        'dimensions': 1024,
    }


@pytest.mark.asyncio
async def test_compatible_rerank_auth_top_k_and_invalid_indices(setup, monkeypatch):
    _, memory, _ = setup
    requests = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {'results': [
                {'index': 0, 'relevance_score': 0.4},
                {'index': 1, 'relevance_score': 0.9},
                {'index': 1, 'relevance_score': 1.0},
                {'index': 9, 'relevance_score': 2.0},
                {'index': True, 'relevance_score': 3.0},
            ]}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return Response()

    monkeypatch.setattr('evocode.memory.service.httpx.AsyncClient', Client)
    monkeypatch.setenv('RERANK_TEST_KEY', 'mock-token')
    cfg = memory.config.raw_rag.model_copy(update={
        'rerank_provider': 'compatible',
        'rerank_api_key_env': 'RERANK_TEST_KEY',
        'rerank_top_k': 2,
        'rerank_model': 'rerank-test',
        'rerank_url': 'https://offline.invalid/rerank',
    })
    rows = [{'text': f'doc-{i}', 'id': i} for i in range(4)]

    ranked = await memory._rerank('query', rows, cfg)

    assert [row['id'] for row in ranked] == [1, 0, 2, 3]
    url, request = requests[0]
    assert url == cfg.rerank_url
    assert request['headers']['Authorization'] == 'Bearer mock-token'
    assert request['json'] == {
        'model': 'rerank-test',
        'query': 'query',
        'documents': ['doc-0', 'doc-1'],
        'top_n': 2,
    }


@pytest.mark.asyncio
async def test_dashscope_rerank_request_and_output_shape(setup, monkeypatch):
    _, memory, _ = setup
    requests = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {'output': {'results': [
                {'index': 0, 'relevance_score': 0.1},
                {'index': 1, 'relevance_score': 0.8},
            ]}}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return Response()

    monkeypatch.setattr('evocode.memory.service.httpx.AsyncClient', Client)
    monkeypatch.setenv('RERANK_TEST_KEY', 'mock-token')
    cfg = memory.config.raw_rag.model_copy(update={
        'rerank_provider': 'dashscope',
        'rerank_api_key_env': 'RERANK_TEST_KEY',
        'rerank_top_k': 5,
        'rerank_model': 'bailian-rerank-test',
        'rerank_url': 'https://offline.invalid/api/v1/services/rerank/text-rerank/text-rerank',
    })
    rows = [{'text': 'first'}, {'text': 'second'}]

    ranked = await memory._rerank('query', rows, cfg)

    assert [row['text'] for row in ranked] == ['second', 'first']
    url, request = requests[0]
    assert url == cfg.rerank_url
    assert request['headers']['Authorization'] == 'Bearer mock-token'
    assert request['json'] == {
        'model': 'bailian-rerank-test',
        'input': {'query': 'query', 'documents': ['first', 'second']},
        'parameters': {'top_n': 2},
    }


def test_episode_indexing_groups_causal_events_with_stable_provenance(setup, tmp_path):
    store, memory, task = setup
    source = tmp_path / 'service.py'
    source.write_text('class TokenService:\n    def refresh(self):\n        return True\n')
    failed = store.append_event(task['task_id'], 'tool.failed', {
        'error': 'refresh token failure', 'file_paths': ['service.py'], 'symbols': ['TokenService'],
    })
    patch = store.save_artifact('--- a/service.py\n+++ b/service.py\n@@\n-    return False\n+    return True\n', kind='code_patch')
    store.append_event(task['task_id'], 'code.patch', {
        'file_paths': ['service.py'], 'patch_artifact_id': patch['artifact_id'],
    })
    verified = store.append_event(task['task_id'], 'test.result', {
        'passed': True, 'output': 'refresh token test passed', 'file_paths': ['service.py'],
    })
    anchor = next(e for e in store.read_events(task_id=task['task_id']) if e['event_id'] == verified['event_id'])

    chunks = list(memory._make_chunks({
        **anchor,
        'payload_json': json.dumps(anchor['payload']),
    }))
    episodes = [chunk for chunk in chunks if chunk['metadata']['chunk_type'] == 'episode']

    assert episodes
    assert failed['event_id'] in episodes[0]['metadata']['event_ids']
    assert verified['event_id'] in episodes[0]['metadata']['event_ids']
    assert episodes[0]['metadata']['event_count'] == len(episodes[0]['metadata']['event_ids'])
    assert episodes[0]['metadata']['file_paths'] == ['service.py']
    assert episodes[0]['metadata']['symbols'] == ['TokenService']
    assert episodes[0]['chunk_id'] == list(memory._make_episode({
        **anchor,
        'payload': anchor['payload'],
    }))[0]['chunk_id']


def test_applicability_exposes_conservative_detailed_checks(setup, tmp_path):
    _, memory, task = setup
    source = tmp_path / 'app.py'
    source.write_text('def current_name():\n    return 1\n')
    metadata = {
        'repository_id': task['repository_id'],
        'file_paths': ['app.py'],
        'symbols': ['removed_name'],
        'content_evidence': [{'path': 'app.py', 'sha256': 'old-content'}],
        'dependency_evidence': [],
        'git_commit': None,
    }

    result = memory._applicability(metadata, task)

    assert result['status'] == 'stale'
    assert result['checks']['paths']['status'] == 'valid'
    assert result['checks']['content']['status'] == 'stale'
    assert result['checks']['symbols']['status'] == 'stale'
    assert result['checks']['dependencies']['status'] == 'unknown'
    assert result['reasons']


def test_applicability_stays_unknown_without_comparable_source_evidence(setup):
    _, memory, task = setup

    result = memory._applicability({
        'repository_id': task['repository_id'], 'file_paths': [], 'symbols': [],
        'content_evidence': [], 'dependency_evidence': [], 'git_commit': None,
    }, task)

    assert result['status'] == 'unknown'
    assert result['checks']['paths']['status'] == 'unknown'
    assert result['checks']['dependencies']['status'] == 'unknown'


@pytest.mark.asyncio
async def test_episode_upserts_and_rebuild_are_idempotent(setup, monkeypatch):
    store, memory, task = setup
    store.append_event(task['task_id'], 'tool.failed', {'error': 'package import failed'})
    store.append_event(task['task_id'], 'code.patch', {'patch': 'add import'} )
    store.append_event(task['task_id'], 'test.result', {'passed': True, 'output': 'package import passed'})

    async def offline_upsert(*args):
        return None

    monkeypatch.setattr(memory, '_upsert', offline_upsert)
    assert (await memory.index_pending())['failed'] == 0
    first_count = memory.status()['chunks']
    assert first_count > 3

    assert (await memory.rebuild_index())['failed'] == 0
    assert memory.status()['chunks'] == first_count


@pytest.mark.asyncio
async def test_episode_spans_to_verification_and_uses_event_captured_content_hash(setup, tmp_path, monkeypatch):
    store, memory, task = setup
    source = tmp_path / 'service.py'
    original = b'def refresh():\n    return False\n'
    source.write_bytes(original)
    import hashlib
    expected_hash = hashlib.sha256(original).hexdigest()
    failed = store.append_event(task['task_id'], 'tool.failed', {
        'error': 'refresh failed', 'file_paths': ['service.py'],
        'metadata': {'content_evidence': [{'path': 'service.py', 'sha256': expected_hash, 'source': 'event_snapshot'}]},
    })
    for index in range(8):
        store.append_event(task['task_id'], 'tool.completed', {
            'output': f'inspection {index}', 'file_paths': ['service.py'],
        })
    patch = store.save_artifact('--- a/service.py\n+++ b/service.py\n@@\n-    return False\n+    return True\n', kind='code_patch')
    store.append_event(task['task_id'], 'code.patch', {
        'file_paths': ['service.py'], 'patch_artifact_id': patch['artifact_id'],
    })
    verified = store.append_event(task['task_id'], 'test.result', {'passed': True, 'output': 'refresh passed'})
    source.write_text('def refresh():\n    return True\n')

    async def offline_upsert(*args):
        return None

    monkeypatch.setattr(memory, '_upsert', offline_upsert)
    assert (await memory.index_pending())['failed'] == 0
    with store.transaction() as db:
        row = db.execute("SELECT chunk_id,metadata_json FROM memory_chunks WHERE json_extract(metadata_json,'$.chunk_type')='episode'").fetchone()
    metadata = json.loads(row['metadata_json'])
    assert failed['event_id'] in metadata['event_ids']
    assert verified['event_id'] in metadata['event_ids']
    assert metadata['content_evidence'] == [{
        'path': 'service.py', 'sha256': expected_hash, 'source': 'event_snapshot',
    }]
    assert memory._applicability({**metadata, 'repository_id': task['repository_id']}, task)['status'] == 'stale'
    first_count = memory.status()['chunks']

    assert (await memory.rebuild_index())['failed'] == 0
    assert memory.status()['chunks'] == first_count
    with store.transaction() as db:
        rebuilt = db.execute('SELECT metadata_json FROM memory_chunks WHERE chunk_id=?', (row['chunk_id'],)).fetchone()
    assert json.loads(rebuilt['metadata_json'])['content_evidence'] == metadata['content_evidence']


@pytest.mark.asyncio
async def test_runtime_verification_events_finalize_episode_once(setup, monkeypatch):
    store, memory, task = setup
    store.append_event(task['task_id'], 'tool.failed', {'error': 'check failed'})
    store.append_event(task['task_id'], 'verification.result', {'passed': False, 'status': 'failed'})

    async def offline_upsert(*args):
        return None

    monkeypatch.setattr(memory, '_upsert', offline_upsert)
    assert (await memory.index_pending())['failed'] == 0
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM memory_chunks WHERE json_extract(metadata_json,'$.chunk_type')='episode'").fetchone()[0] == 0

    store.append_event(task['task_id'], 'code.patch', {'patch': 'repair', 'file_paths': ['module.py']})
    verified = store.append_event(task['task_id'], 'verification.result', {'passed': True, 'status': 'passed'})
    assert (await memory.index_pending())['failed'] == 0
    with store.transaction() as db:
        rows = list(db.execute("SELECT chunk_id,metadata_json FROM memory_chunks WHERE json_extract(metadata_json,'$.chunk_type')='episode'"))
    assert rows
    metadata = json.loads(rows[0]['metadata_json'])
    assert 'tool.failed' in metadata['event_types']
    assert metadata['event_ids'][-1] == verified['event_id']
    with store.transaction() as db:
        episode_count = db.execute("SELECT count(*) FROM memory_chunks WHERE json_extract(metadata_json,'$.chunk_type')='episode'").fetchone()[0]

    store.append_event(task['task_id'], 'assistant.final', {'content': 'completed'})
    assert (await memory.index_pending())['failed'] == 0
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM memory_chunks WHERE json_extract(metadata_json,'$.chunk_type')='episode'").fetchone()[0] == episode_count


@pytest.mark.asyncio
async def test_episode_retrieval_reserves_failure_patch_and_verification_excerpts(setup, monkeypatch):
    store, memory, task = setup
    store.append_event(task['task_id'], 'tool.failed', {'error': 'FAILURE_MARKER authentication failed'})
    internal = store.append_event(task['task_id'], 'task.state.delta', {'private_state': 'INTERNAL_STATE_MARKER ' + 'secret ' * 100})
    store.append_event(task['task_id'], 'tool.completed', {
        'output': 'MIDDLE_NOISE_MARKER ' + 'routine-output ' * 1800,
    })
    store.append_event(task['task_id'], 'code.patch', {'patch': 'PATCH_MARKER replace expired-token handling'})
    store.append_event(task['task_id'], 'verification.result', {
        'passed': True, 'status': 'passed', 'output': 'FINAL_VERIFICATION_MARKER all checks passed',
    })

    async def offline_upsert(*args):
        return None

    monkeypatch.setattr(memory, '_upsert', offline_upsert)
    assert (await memory.index_pending())['failed'] == 0
    with store.transaction() as db:
        episode = db.execute(
            "SELECT chunk_id FROM memory_chunks WHERE json_extract(metadata_json,'$.chunk_type')='episode' AND text LIKE '%MIDDLE_NOISE_MARKER%' LIMIT 1"
        ).fetchone()
        assert episode
        raw = db.execute(
            "SELECT chunk_id FROM memory_chunks WHERE json_extract(metadata_json,'$.chunk_type')='event' AND text LIKE '%MIDDLE_NOISE_MARKER%' LIMIT 1"
        ).fetchone()
        assert raw

    async def dense(*args):
        return [raw['chunk_id'], episode['chunk_id']]

    monkeypatch.setattr(memory, '_dense', dense)
    task['config']['raw_rag']['max_context_tokens'] = 1200
    result = await memory.search('MIDDLE_NOISE_MARKER', _new_search_task(store, task))

    assert result.evidence
    evidence = result.evidence[0]
    assert 'FAILURE_MARKER' in evidence.text
    assert 'PATCH_MARKER' in evidence.text
    assert 'FINAL_VERIFICATION_MARKER' in evidence.text
    assert 'INTERNAL_STATE_MARKER' not in evidence.text
    assert sum(len(item.text.encode('utf-8')) for item in result.evidence) <= 1200
    assert internal['event_id'] not in evidence.metadata['related_event_ids']
