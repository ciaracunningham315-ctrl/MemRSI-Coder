import threading
import time

from evocode.config import AppConfig
from evocode.contracts import TaskSpec
from evocode.persistence.store import EventStore


def _store(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    store = EventStore(tmp_path / 'events.sqlite')
    config = AppConfig(data_dir=str(tmp_path / 'data'))
    session = store.create_session(str(repo), 'Replay session')
    task = store.create_task(TaskSpec(
        repo_path=str(repo), prompt='Inspect the repository',
        session_id=session['session']['session_id'],
    ), config.snapshot())
    state = dict(task['state'])
    state.update(next_node='coder', planner_reason='continue', custom_replay_field={'ok': True})
    store.update_task(task['task_id'], status='interrupted', state=state)
    store.append_event(task['task_id'], 'tool.started', {
        'tool_call_id': 'side-effect-1', 'tool': 'run_command',
        'arguments': {'command': ['touch', 'must-not-run']}, 'prepared': {},
    })
    store.append_session_event(session['session']['session_id'], 'user.message', {
        'content': 'Follow up',
    }, task_id=task['task_id'], client_message_id='message-2')
    return store, repo, session['session']['session_id'], task['task_id']


def test_rebuild_restores_lost_materialized_rows_and_never_replays_tool(tmp_path):
    store, repo, session_id, task_id = _store(tmp_path)
    with store.transaction() as db:
        db.execute("DELETE FROM events WHERE task_id=? AND event_type='task.updated'", (task_id,))
        db.execute('DELETE FROM checkpoints WHERE task_id=?', (task_id,))
        db.execute('DELETE FROM conversation_messages')
        db.execute('DELETE FROM message_receipts')
        db.execute('DELETE FROM tasks')
        db.execute('DELETE FROM sessions')
        db.execute('DELETE FROM repositories')

    result = store.rebuild_materialized_state()

    assert result['sessions'] == 1
    assert result['tasks'] == 1
    task = store.get_task(task_id)
    assert task['status'] == 'interrupted'
    assert task['state']['custom_replay_field'] == {'ok': True}
    session = store.get_session(session_id)
    assert session['session']['repo_path'] == str(repo)
    assert [message['content'] for message in session['messages']] == ['Inspect the repository', 'Follow up']
    assert session['session']['revision'] == max(
        event['seq'] for event in store.read_events(session_id=session_id)
        if event['event_type'] == 'user.message'
    )
    assert store.get_message_receipt(session_id, 'message-2')['task_id'] == task_id
    assert not (repo / 'must-not-run').exists()
    assert len([e for e in store.read_events(task_id=task_id) if e['event_type'] == 'tool.started']) == 1
    store.close()


def test_legacy_database_backfills_replay_metadata_before_projection_loss(tmp_path):
    store, repo, session_id, task_id = _store(tmp_path)
    source = next(event for event in store.read_events(task_id=task_id) if event['event_type'] == 'user.message')
    store.link_message_receipt(session_id, 'legacy-receipt', source['event_id'])
    with store.transaction() as db:
        db.execute("DELETE FROM events WHERE event_type IN ('session.created','task.created','message.receipt.linked')")

    result = store.rebuild_materialized_state()

    assert result['tasks'] == 1
    row = store.connection.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='task.created'", (task_id,)).fetchone()
    import json
    created = json.loads(row['payload_json'])
    assert created['prompt'] == 'Inspect the repository'
    assert created['config'] == store.get_task(task_id)['config']
    assert store.get_session(session_id)['session']['repo_path'] == str(repo)
    assert store.get_message_receipt(session_id, 'legacy-receipt')['event_id'] == source['event_id']
    assert store.connection.execute("SELECT 1 FROM events WHERE event_type='message.receipt.linked'").fetchone()
    store.close()


def test_legacy_baseline_prevents_stale_history_from_overwriting_live_rows(tmp_path):
    store, _, _, task_id = _store(tmp_path)
    old = store.get_task(task_id)
    first = dict(old['state'])
    first['legacy_value'] = 'old'
    store.update_task(task_id, status='running', state=first)
    latest = dict(first)
    latest['legacy_value'] = 'current'
    latest['current_only'] = True
    store.update_task(task_id, status='interrupted', state=latest)
    with store.transaction() as db:
        db.execute("DELETE FROM events WHERE task_id=? AND event_type IN ('task.created','task.state.delta')", (task_id,))

    store.rebuild_materialized_state()

    rebuilt = store.get_task(task_id)
    assert rebuilt['status'] == 'interrupted'
    assert rebuilt['state']['legacy_value'] == 'current'
    assert rebuilt['state']['current_only'] is True
    store.close()


def test_internal_projection_events_are_not_queued_for_memory_indexing(tmp_path):
    store, _, _, task_id = _store(tmp_path)
    task = store.get_task(task_id)
    state = dict(task['state'])
    state['scratch'] = 'private prompt-bearing data'
    store.update_task(task_id, state=state)
    session_id = task['session_id']
    store.update_session_context(session_id, 'summary', 1, {'private': 'context'})
    store.append_session_event(session_id, 'message.receipt.linked', {
        'client_message_id': 'receipt-3', 'event_id': 'missing-user-event',
    })

    indexed = store.connection.execute(
        'SELECT e.event_type FROM index_jobs j JOIN events e USING(event_id)'
    ).fetchall()
    assert all(row['event_type'] not in {
        'task.created', 'task.state.delta', 'task.updated', 'session.created',
        'session.context.updated', 'message.receipt.linked',
    } for row in indexed)
    store.close()


def test_resume_state_uses_latest_domain_snapshot_not_checkpoint(tmp_path):
    store, _, _, task_id = _store(tmp_path)
    task = store.get_task(task_id)
    stale = dict(task['state'])
    stale['custom_replay_field'] = {'ok': False}
    store.save_checkpoint(task_id, stale)

    recovered = store.replay_task_state(task_id, fallback=stale)

    assert recovered['status'] == 'interrupted'
    assert recovered['custom_replay_field'] == {'ok': True}
    store.close()


def test_event_store_uses_wal_and_waits_for_other_connection_writer(tmp_path):
    database = tmp_path / 'shared-events.sqlite'
    first = EventStore(database)
    second = EventStore(database)
    assert first.connection.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'
    assert first.connection.execute('PRAGMA busy_timeout').fetchone()[0] == 30000
    assert second.connection.execute('PRAGMA busy_timeout').fetchone()[0] == 30000

    with first.transaction() as connection:
        connection.execute('CREATE TABLE concurrency_probe (writer TEXT PRIMARY KEY)')

    writer_holds_lock = threading.Event()
    release_writer = threading.Event()
    second_started = threading.Event()
    second_finished = threading.Event()
    errors = []

    def first_writer():
        try:
            with first.transaction() as connection:
                connection.execute('INSERT INTO concurrency_probe VALUES (?)', ('first',))
                writer_holds_lock.set()
                if not release_writer.wait(timeout=5):
                    raise TimeoutError('test did not release the first SQLite writer')
        except BaseException as exc:
            errors.append(exc)

    def second_writer():
        second_started.set()
        try:
            with second.transaction() as connection:
                connection.execute('INSERT INTO concurrency_probe VALUES (?)', ('second',))
        except BaseException as exc:
            errors.append(exc)
        finally:
            second_finished.set()

    first_thread = threading.Thread(target=first_writer)
    second_thread = threading.Thread(target=second_writer)
    first_thread.start()
    assert writer_holds_lock.wait(timeout=2)
    second_thread.start()
    assert second_started.wait(timeout=2)
    time.sleep(0.1)
    assert not second_finished.is_set()
    release_writer.set()
    first_thread.join(timeout=3)
    second_thread.join(timeout=3)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert not errors
    assert {row[0] for row in first.connection.execute(
        'SELECT writer FROM concurrency_probe'
    )} == {'first', 'second'}
    first.close()
    second.close()
