from __future__ import annotations
import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from evocode.contracts import ConversationMessage, ExecutionEvent, Session, TaskState


_NON_INDEXED_EVENT_TYPES = {
    'task.created', 'task.state.delta', 'task.updated',
    'session.created', 'session.context.updated', 'message.receipt.linked',
}


def now():
    return datetime.now(timezone.utc).isoformat()


def decode(row):
    if row is None:
        return None
    result = dict(row)
    for key in list(result):
        if key.endswith('_json'):
            result[key[:-5]] = json.loads(result.pop(key))
    if 'event_type' in result:
        prefix=result['event_type'].split('.')[0]
        result['node']=result.get('payload',{}).get('node') or (prefix if prefix in ('planner','coder','tool') else 'controller')
    return result


def _append_metadata_event(db, session_id, task_id, repository_id, event_type, payload):
    """Persist creation metadata outside the user-visible session sequence."""
    seq = db.execute('SELECT COALESCE(MIN(seq),0)-1 FROM events WHERE session_id=? AND seq<0', (session_id,)).fetchone()[0]
    event_id = str(uuid.uuid4())
    db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)', (
        event_id, session_id, seq, task_id, repository_id, event_type,
        json.dumps(payload, ensure_ascii=False), now(),
    ))
    return event_id


class EventStore:
    def __init__(self, db_path, artifacts_dir=None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir = Path(artifacts_dir or self.db_path.parent / 'artifacts')
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # EventStore transactions are serialized by the instance lock. SQLite's
        # busy handler queues writers from other connections/processes for up
        # to 30 seconds before failing with SQLITE_BUSY.
        self.connection = sqlite3.connect(str(self.db_path), timeout=30.0,
            check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.execute('PRAGMA busy_timeout=30000')
        self.connection.execute('CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY)')
        for file in sorted((Path(__file__).parent / 'migrations').glob('*.sql')):
            if not self.connection.execute('SELECT 1 FROM schema_migrations WHERE name=?', (file.name,)).fetchone():
                migration_name = file.name.replace("'", "''")
                self.connection.executescript('BEGIN IMMEDIATE;\n' + file.read_text() +
                    "\nINSERT INTO schema_migrations VALUES('" + migration_name + "');\nCOMMIT;")

    @contextmanager
    def transaction(self):
        with self._lock:
            nested = self.connection.in_transaction
            savepoint = 'sp_' + uuid.uuid4().hex
            self.connection.execute('SAVEPOINT ' + savepoint if nested else 'BEGIN IMMEDIATE')
            try:
                yield self.connection
                self.connection.execute('RELEASE SAVEPOINT ' + savepoint if nested else 'COMMIT')
            except BaseException:
                if nested:
                    self.connection.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
                    self.connection.execute('RELEASE SAVEPOINT ' + savepoint)
                else:
                    self.connection.execute('ROLLBACK')
                raise

    def create_task(self, spec, config_snapshot):
        root = str(Path(spec.repo_path).resolve())
        if not Path(root).is_dir():
            raise ValueError('Repository directory does not exist')
        repository_id = spec.repository_id or hashlib.sha256(root.encode()).hexdigest()[:24]
        session_id, task_id = spec.session_id or str(uuid.uuid4()), str(uuid.uuid4())
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO repositories(repository_id,root_path) VALUES(?,?)', (repository_id,root))
            session = db.execute('SELECT * FROM sessions WHERE session_id=?', (session_id,)).fetchone()
            if session and session['repository_id'] != repository_id:
                raise ValueError('Session belongs to a different repository')
            db.execute('INSERT OR IGNORE INTO sessions(session_id,repository_id,created_at,title,updated_at) VALUES(?,?,?,?,?)', (session_id,repository_id,now(),spec.prompt[:80],now()))
            if session is None:
                root_path = db.execute('SELECT root_path FROM repositories WHERE repository_id=?', (repository_id,)).fetchone()[0]
                _append_metadata_event(db, session_id, None, repository_id, 'session.created', {
                    'session_id': session_id, 'repository_id': repository_id,
                    'repo_path': root_path, 'title': spec.prompt[:80],
                    'created_at': now(),
                })
            state = TaskState(task_id=task_id, session_id=session_id).model_dump(exclude_none=True)
            state['acceptance_command'] = spec.acceptance_command
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)', (task_id,session_id,repository_id,root,spec.prompt,'created',json.dumps(state),json.dumps(config_snapshot),now(),now()))
            _append_metadata_event(db, session_id, task_id, repository_id, 'task.created', {
                'task_id': task_id, 'session_id': session_id,
                'repository_id': repository_id, 'repo_path': root,
                'prompt': spec.prompt, 'config': config_snapshot,
                'status': 'created', 'state': state,
            })
            self.append_event(task_id,'user.message',{'content':spec.prompt})
        return self.get_task(task_id)

    def create_session(self, repo_path, title=''):
        root = str(Path(repo_path).expanduser().resolve())
        if not Path(root).is_dir():
            raise ValueError('Workspace directory does not exist')
        repository_id = hashlib.sha256(root.encode()).hexdigest()[:24]
        session_id = str(uuid.uuid4())
        timestamp = now()
        with self.transaction() as db:
            db.execute('INSERT OR IGNORE INTO repositories(repository_id,root_path) VALUES(?,?)', (repository_id, root))
            db.execute('INSERT INTO sessions(session_id,repository_id,created_at,title,updated_at) VALUES(?,?,?,?,?)', (session_id, repository_id, timestamp, title or Path(root).name, timestamp))
            _append_metadata_event(db, session_id, None, repository_id, 'session.created', {
                'session_id': session_id, 'repository_id': repository_id,
                'repo_path': root, 'title': title or Path(root).name,
                'created_at': timestamp,
            })
        return self.get_session(session_id)

    def get_session(self, session_id):
        with self._lock:
            row = self.connection.execute('SELECT s.*,r.root_path FROM sessions s JOIN repositories r USING(repository_id) WHERE s.session_id=?', (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        session = decode(row)
        session['repo_path'] = session.pop('root_path')
        tasks = [self.get_task(task_id) for (task_id,) in self.connection.execute('SELECT task_id FROM tasks WHERE session_id=? ORDER BY created_at', (session_id,)).fetchall()]
        failed_tasks = [task for task in tasks if task['status'] == 'failed']
        working = dict(session.get('working_state') or {})
        persisted_failure = working.get('last_failed_task')
        latest_failure = failed_tasks[-1] if failed_tasks else None
        # Older databases can have failed tasks without a session-level diagnostic.
        # Derive one on read, without rewriting the session or exposing task history.
        if latest_failure and (not isinstance(persisted_failure, dict)
                or persisted_failure.get('task_id') != latest_failure['task_id']):
            working['last_failed_task'] = self._failure_diagnostic(
                latest_failure,
                self._patch_file_paths(latest_failure['task_id']),
            )
        session['working_state'] = working
        session['active_task_id'] = next((task['task_id'] for task in reversed(tasks) if task['status'] not in ('completed','failed','cancelled')), None)
        Session.model_validate(session)
        return {'session': session, 'tasks': tasks, 'messages': self.read_conversation_messages(session_id), 'events': self.read_events(session_id=session_id)}

    def _patch_file_paths(self, task_id, db=None):
        query = 'SELECT payload_json FROM events WHERE task_id=? AND event_type=? ORDER BY seq'
        rows = (db or self.connection).execute(query, (task_id, 'code.patch')).fetchall()
        paths = []
        for row in rows:
            payload = json.loads(row['payload_json'])
            file_paths = payload.get('file_paths') or []
            if isinstance(file_paths, list):
                paths.extend(file_paths)
        return list(dict.fromkeys(str(path) for path in paths))

    @staticmethod
    def _failure_diagnostic(task, patch_paths=()):
        state = task.get('state') or {}
        report = state.get('last_coder_report') or {}
        if hasattr(report, 'model_dump'):
            report = report.model_dump()
        if not isinstance(report, dict):
            report = {}
        verification = state.get('verification') or {}
        if not isinstance(verification, dict):
            verification = {}
        changed_files = report.get('changed_files')
        if not isinstance(changed_files, list) or not changed_files:
            changed_files = list(patch_paths)
        if not isinstance(changed_files, list):
            changed_files = []
        safe_files = []
        total_path_chars = 0
        for path in changed_files:
            item = str(path)[:120]
            if total_path_chars + len(item) > 500 or len(safe_files) >= 10:
                break
            safe_files.append(item)
            total_path_chars += len(item)
        diagnostic = {
            'task_id': task['task_id'],
            'status': 'failed',
            'error': str(state.get('error') or 'Task failed')[:700],
            'changed_files': safe_files,
            'verification': {
                key: verification[key]
                for key in ('status', 'command', 'coverage', 'attempts')
                if key in verification and isinstance(verification[key], (str, int, float, bool, type(None)))
            },
        }
        if 'attempts' not in diagnostic['verification'] and state.get('verification_attempts'):
            diagnostic['verification']['attempts'] = state['verification_attempts']
        # Keep the durable context value comfortably below the assembler's 6 KB cap.
        diagnostic['error'] = diagnostic['error'][:600]
        if isinstance(diagnostic['verification'].get('command'), str):
            diagnostic['verification']['command'] = diagnostic['verification']['command'][:250]
        return diagnostic

    def list_sessions(self):
        with self._lock:
            ids = [row[0] for row in self.connection.execute('SELECT session_id FROM sessions ORDER BY updated_at DESC,created_at DESC').fetchall()]
        return [{**detail['session'], 'tasks': detail['tasks']} for detail in (self.get_session(session_id) for session_id in ids)]

    def append_session_event(self, session_id, event_type, payload, task_id=None, client_message_id=None):
        with self.transaction() as db:
            payload = dict(payload)
            if client_message_id:
                payload['client_message_id'] = client_message_id
            if client_message_id:
                prior = db.execute('SELECT event_id FROM message_receipts WHERE session_id=? AND client_message_id=?', (session_id, client_message_id)).fetchone()
                if prior:
                    return decode(db.execute('SELECT * FROM events WHERE event_id=?', (prior['event_id'],)).fetchone())
            session = db.execute('SELECT repository_id FROM sessions WHERE session_id=?', (session_id,)).fetchone()
            if session is None:
                raise KeyError(session_id)
            if task_id:
                task = db.execute('SELECT session_id,repository_id FROM tasks WHERE task_id=?', (task_id,)).fetchone()
                if task is None or task['session_id'] != session_id:
                    raise ValueError('Task does not belong to this session')
                repository_id = task['repository_id']
            else:
                repository_id = session['repository_id']
            seq = db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE session_id=? AND seq>0', (session_id,)).fetchone()[0]
            event_id = str(uuid.uuid4())
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)', (event_id, session_id, seq, task_id, repository_id, event_type, json.dumps(payload,ensure_ascii=False), now()))
            ExecutionEvent(event_id=event_id,session_id=session_id,task_id=task_id,seq=seq,event_type=event_type,payload=payload)
            self._project_message(db, event_id, session_id, task_id, event_type, payload, seq)
            if event_type not in _NON_INDEXED_EVENT_TYPES:
                db.execute('INSERT INTO index_jobs(job_id,event_id) VALUES(?,?)', (str(uuid.uuid4()), event_id))
            if client_message_id:
                db.execute('INSERT INTO message_receipts(session_id,client_message_id,event_id,task_id) VALUES(?,?,?,?)', (session_id, client_message_id, event_id, task_id))
            db.execute('UPDATE sessions SET updated_at=? WHERE session_id=?', (now(), session_id))
            return decode(db.execute('SELECT * FROM events WHERE event_id=?', (event_id,)).fetchone())

    def get_message_receipt(self, session_id, client_message_id):
        with self._lock:
            row = self.connection.execute('SELECT event_id,task_id FROM message_receipts WHERE session_id=? AND client_message_id=?', (session_id, client_message_id)).fetchone()
        return dict(row) if row else None

    def link_message_receipt(self, session_id, client_message_id, event_id):
        with self.transaction() as db:
            event=db.execute("SELECT task_id FROM events WHERE event_id=? AND session_id=? AND event_type='user.message'",(event_id,session_id)).fetchone()
            if event is None:
                raise ValueError('Receipt must refer to a user message in this session')
            db.execute('INSERT INTO message_receipts VALUES(?,?,?,?)',(session_id,client_message_id,event_id,event['task_id']))
            self.append_session_event(session_id, 'message.receipt.linked', {
                'client_message_id': client_message_id, 'event_id': event_id,
            }, task_id=event['task_id'])

    def get_task(self, task_id):
        with self._lock:
            task = decode(self.connection.execute('SELECT * FROM tasks WHERE task_id=?',(task_id,)).fetchone())
        if task is None:
            raise KeyError(task_id)
        return task

    def list_tasks(self):
        with self._lock:
            return [decode(r) for r in self.connection.execute('SELECT * FROM tasks ORDER BY created_at DESC')]

    def update_task(self, task_id, status=None, state=None):
        with self.transaction() as db:
            old=self.get_task(task_id)
            value = dict(state if state is not None else old['state'])
            value.update(task_id=task_id, session_id=old['session_id'], status=status or old['status'])
            TaskState.model_validate(value)
            self.append_event(task_id, 'task.state.delta', self._state_delta(old['state'], value, status or old['status']))
            db.execute('UPDATE tasks SET status=?,state_json=?,updated_at=? WHERE task_id=?',(status or old['status'],json.dumps(value),now(),task_id))
            self.append_event(task_id,'task.updated',{'status':status or old['status'],'state':value})
        return self.get_task(task_id)

    @staticmethod
    def _state_delta(before, after, status):
        before = before if isinstance(before, dict) else {}
        after = after if isinstance(after, dict) else {}
        return {
            'set': {key: value for key, value in after.items() if key not in before or before[key] != value},
            'remove': sorted(set(before) - set(after)),
            'status': status,
        }

    def fail_task(self, task_id, state, error=None):
        """Persist a failed task and its bounded session diagnostic atomically."""
        with self.transaction() as db:
            task = self.get_task(task_id)
            value = dict(state)
            value.update(task_id=task_id, session_id=task['session_id'], status='failed')
            if error is not None:
                value['error'] = str(error)

            task_for_diagnostic = {**task, 'state': value}
            diagnostic = self._failure_diagnostic(task_for_diagnostic, self._patch_file_paths(task_id, db))
            TaskState.model_validate(value)
            self.append_event(task_id, 'task.state.delta', self._state_delta(task['state'], value, 'failed'))
            event_id = str(uuid.uuid4())
            seq = db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE session_id=? AND seq>0', (task['session_id'],)).fetchone()[0]
            payload = {'type': 'task.failed', 'diagnostic': diagnostic, 'status': 'failed'}
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)', (
                event_id, task['session_id'], seq, task_id, task['repository_id'],
                'task.failed', json.dumps(payload, ensure_ascii=False), now(),
            ))
            ExecutionEvent(event_id=event_id, session_id=task['session_id'], task_id=task_id,
                seq=seq, event_type='task.failed', payload=payload)
            db.execute('INSERT INTO index_jobs(job_id,event_id) VALUES(?,?)', (str(uuid.uuid4()), event_id))
            db.execute('UPDATE tasks SET status=?,state_json=?,updated_at=? WHERE task_id=?',
                ('failed', json.dumps(value, ensure_ascii=False), now(), task_id))
            session = decode(db.execute('SELECT * FROM sessions WHERE session_id=?', (task['session_id'],)).fetchone())
            working = dict(session['working_state'] or {})
            working['last_failed_task'] = diagnostic
            db.execute('UPDATE sessions SET working_state_json=?,updated_at=? WHERE session_id=?',
                (json.dumps(working, ensure_ascii=False), now(), task['session_id']))
            self._project_message(db, event_id, task['session_id'], task_id, 'task.failed', payload, seq)
        return self.get_task(task_id)

    def append_event(self, task_id, event_type, payload):
        with self.transaction() as db:
            task=self.get_task(task_id)
            seq=db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE session_id=? AND seq>0',(task['session_id'],)).fetchone()[0]
            event_id=str(uuid.uuid4())
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)',(event_id,task['session_id'],seq,task_id,task['repository_id'],event_type,json.dumps(payload,ensure_ascii=False),now()))
            ExecutionEvent(event_id=event_id,session_id=task['session_id'],task_id=task_id,seq=seq,event_type=event_type,node=event_type.split('.')[0],payload=payload)
            self._project_message(db, event_id, task['session_id'], task_id, event_type, payload, seq)
            db.execute('UPDATE sessions SET updated_at=? WHERE session_id=?', (now(), task['session_id']))
            if event_type not in _NON_INDEXED_EVENT_TYPES:
                db.execute('INSERT INTO index_jobs(job_id,event_id) VALUES(?,?)',(str(uuid.uuid4()),event_id))
            return decode(db.execute('SELECT * FROM events WHERE event_id=?',(event_id,)).fetchone())

    def _project_message(self, db, event_id, session_id, task_id, event_type, payload, seq):
        kinds = {'user.message': 'user', 'assistant.progress': 'progress', 'assistant.question': 'question', 'assistant.final': 'final'}
        if event_type not in kinds:
            return
        created_at = db.execute('SELECT created_at FROM events WHERE event_id=?', (event_id,)).fetchone()[0]
        message = ConversationMessage(message_id=event_id,session_id=session_id,task_id=task_id,
            role='user' if event_type == 'user.message' else 'assistant',kind=kinds[event_type],
            content=str(payload.get('content','')),seq=seq,created_at=created_at)
        db.execute('INSERT INTO conversation_messages VALUES(?,?,?,?,?,?,?,?)', tuple(message.model_dump().values()))
        if event_type == 'user.message':
            db.execute('UPDATE sessions SET revision=? WHERE session_id=?', (seq,session_id))

    def read_conversation_messages(self, session_id, after_seq=0, before_seq=None, limit=None):
        clauses=['session_id=?','seq>?']; args=[session_id,after_seq]
        if before_seq is not None:
            clauses.append('seq<?'); args.append(before_seq)
        query='SELECT * FROM conversation_messages WHERE '+' AND '.join(clauses)+' ORDER BY seq'
        if limit is not None:
            if not isinstance(limit,int) or isinstance(limit,bool) or limit < 1:
                raise ValueError('Message limit must be a positive integer')
            query+=' LIMIT ?'; args.append(limit)
        with self._lock:
            return [ConversationMessage.model_validate(dict(row)).model_dump() for row in self.connection.execute(query,args)]

    def update_session_context(self, session_id, summary, summary_through_seq, working_state=None):
        with self.transaction() as db:
            old=decode(db.execute('SELECT * FROM sessions WHERE session_id=?',(session_id,)).fetchone())
            if old is None:
                raise KeyError(session_id)
            if summary_through_seq < old['summary_through_seq']:
                return
            value = working_state if working_state is not None else old['working_state']
            db.execute('UPDATE sessions SET summary=?,summary_through_seq=?,working_state_json=? WHERE session_id=?',
                (summary,summary_through_seq,json.dumps(value,ensure_ascii=False),session_id))
            self.append_session_event(session_id, 'session.context.updated', {
                'summary': summary, 'summary_through_seq': summary_through_seq,
                'working_state': value,
            })

    def publish_final(self, task_id, content, state):
        """Commit one answer and completion together; None means newer input won."""
        return self._publish_answer(task_id,content,state,'final','completed','end')

    def publish_question(self, task_id, question, state):
        return self._publish_answer(task_id,question,state,'question','waiting_user','waiting_user')

    def _publish_answer(self, task_id, content, state, kind, status, next_node):
        if not content.strip():
            raise ValueError('Assistant answer cannot be empty')
        with self.transaction() as db:
            task=self.get_task(task_id)
            prior=db.execute("SELECT message_id FROM conversation_messages WHERE task_id=? AND kind='final'",(task_id,)).fetchone()
            if prior:
                return task
            latest=db.execute("SELECT COALESCE(MAX(seq),0) FROM events WHERE task_id=? AND event_type='user.message'",(task_id,)).fetchone()[0]
            if latest > state.get('last_user_event_seq',0):
                return None
            event=self.append_event(task_id,'assistant.'+kind,{'content':content})
            state.update(status=status,next_node=next_node)
            if kind == 'final':
                state.update(final_answer=content,final_message_id=event['event_id'],agent_finished=True)
            self.update_task(task_id,status=status,state=state)
            self.append_event(task_id,'node.transition',{'from':'planner','to':next_node,'reason':kind+'_published'})
            if kind == 'final':
                session=decode(db.execute('SELECT * FROM sessions WHERE session_id=?',(task['session_id'],)).fetchone())
                work=dict(session['working_state'])
                work.update(last_task_id=task_id,last_final_message_id=event['event_id'])
                self.update_session_context(task['session_id'],session['summary'],session['summary_through_seq'],work)
            self.save_checkpoint(task_id,state)
        return self.get_task(task_id)

    def read_events(self, task_id=None, session_id=None, after_seq=0):
        clauses=['seq>?']; args=[after_seq]
        for key,value in [('task_id',task_id),('session_id',session_id)]:
            if value is not None:
                clauses.append(key+'=?'); args.append(value)
        with self._lock:
            return [decode(r) for r in self.connection.execute('SELECT * FROM events WHERE '+' AND '.join(clauses)+' ORDER BY session_id,seq',args)]

    def rebuild_materialized_state(self):
        """Rebuild session, task, and conversation projections from the durable event log.

        Older databases did not record creation metadata as events. Before rebuilding,
        capture that metadata from any surviving rows so those databases remain
        recoverable on future runs. This method is explicit; opening a store never
        rewrites live projections automatically.
        """
        with self.transaction() as db:
            # Compatibility bridge for databases written before creation events.
            for row in db.execute('SELECT s.*,r.root_path FROM sessions s LEFT JOIN repositories r USING(repository_id)').fetchall():
                found = db.execute("SELECT 1 FROM events WHERE session_id=? AND event_type='session.created'", (row['session_id'],)).fetchone()
                if not found:
                    through_seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE session_id=? AND seq>0', (row['session_id'],)).fetchone()[0]
                    _append_metadata_event(db, row['session_id'], None, row['repository_id'], 'session.created', {
                        'session_id': row['session_id'], 'repository_id': row['repository_id'],
                        'repo_path': row['root_path'] or '', 'title': row['title'],
                        'created_at': row['created_at'], 'summary': row['summary'],
                        'summary_through_seq': row['summary_through_seq'],
                        'working_state': json.loads(row['working_state_json']),
                        'updated_at': row['updated_at'],
                        'legacy_baseline_through_seq': through_seq,
                    })
            for row in db.execute('SELECT * FROM tasks').fetchall():
                found = db.execute("SELECT 1 FROM events WHERE task_id=? AND event_type='task.created'", (row['task_id'],)).fetchone()
                if not found:
                    through_seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE session_id=? AND seq>0', (row['session_id'],)).fetchone()[0]
                    _append_metadata_event(db, row['session_id'], row['task_id'], row['repository_id'], 'task.created', {
                        'task_id': row['task_id'], 'session_id': row['session_id'],
                        'repository_id': row['repository_id'], 'repo_path': row['repo_path'],
                        'prompt': row['prompt'], 'config': json.loads(row['config_json']),
                        'status': row['status'], 'state': json.loads(row['state_json']),
                        'created_at': row['created_at'],
                        'updated_at': row['updated_at'],
                        'legacy_baseline_through_seq': through_seq,
                    })

            # Receipts in pre-event stores have no event to reconstruct them from.
            # Record links before replacing the receipt projection so they survive.
            for receipt in db.execute('SELECT * FROM message_receipts').fetchall():
                linked = False
                for row in db.execute("SELECT payload_json FROM events WHERE session_id=? AND event_type='message.receipt.linked'", (receipt['session_id'],)).fetchall():
                    payload = json.loads(row['payload_json'])
                    if payload.get('client_message_id') == receipt['client_message_id'] and payload.get('event_id') == receipt['event_id']:
                        linked = True
                        break
                if not linked:
                    source = db.execute("SELECT repository_id FROM events WHERE event_id=? AND session_id=? AND event_type IN ('user.message','conversation.message_linked')", (receipt['event_id'], receipt['session_id'])).fetchone()
                    if source:
                        _append_metadata_event(db, receipt['session_id'], receipt['task_id'], source['repository_id'],
                            'message.receipt.linked', {'client_message_id': receipt['client_message_id'],
                                                       'event_id': receipt['event_id']})

            events = [decode(row) for row in db.execute('SELECT * FROM events ORDER BY session_id,seq').fetchall()]
            session_data = {}
            task_data = {}
            events_by_session = {}
            for event in events:
                events_by_session.setdefault(event['session_id'], []).append(event)
            for event in events:
                payload = event['payload']
                kind = event['event_type']
                if kind == 'session.created':
                    session_data[event['session_id']] = {
                        'session_id': event['session_id'],
                        'repository_id': event.get('repository_id') or payload.get('repository_id'),
                        'repo_path': payload.get('repo_path', ''),
                        'created_at': payload.get('created_at') or event['created_at'],
                        'updated_at': payload.get('updated_at') or event['created_at'],
                        'baseline_through_seq': payload.get('legacy_baseline_through_seq', -1),
                        'title': payload.get('title', ''), 'summary': payload.get('summary', ''),
                        'summary_through_seq': payload.get('summary_through_seq', 0),
                        'working_state': payload.get('working_state', {}),
                    }
                elif kind == 'task.created' and event.get('task_id'):
                    task_data[event['task_id']] = {
                        'task_id': event['task_id'], 'session_id': event['session_id'],
                        'repository_id': event.get('repository_id') or payload.get('repository_id'),
                        'repo_path': payload.get('repo_path', ''), 'prompt': payload.get('prompt', ''),
                        'status': payload.get('status', 'created'), 'state': payload.get('state', {}),
                        'config': payload.get('config', {}),
                        'created_at': payload.get('created_at') or event['created_at'],
                        'updated_at': payload.get('updated_at') or event['created_at'],
                        'baseline_through_seq': payload.get('legacy_baseline_through_seq', -1),
                        'has_delta': False,
                    }
                elif kind == 'session.context.updated':
                    current = session_data.setdefault(event['session_id'], {
                        'session_id': event['session_id'], 'repository_id': event.get('repository_id'),
                        'repo_path': '', 'created_at': event['created_at'], 'title': '',
                        'summary': '', 'summary_through_seq': 0, 'working_state': {},
                    })
                    if event['seq'] > current.get('baseline_through_seq', -1):
                        current.update({key: payload[key] for key in ('summary', 'summary_through_seq', 'working_state') if key in payload})
                if kind == 'task.failed' and isinstance(payload.get('diagnostic'), dict):
                    current = session_data.setdefault(event['session_id'], {
                        'session_id': event['session_id'], 'repository_id': event.get('repository_id'),
                        'repo_path': '', 'created_at': event['created_at'], 'title': '',
                        'summary': '', 'summary_through_seq': 0, 'working_state': {},
                        'baseline_through_seq': -1,
                    })
                    if event['seq'] > current.get('baseline_through_seq', -1):
                        working = dict(current.get('working_state') or {})
                        working['last_failed_task'] = payload['diagnostic']
                        current['working_state'] = working
                if event.get('task_id') and event['task_id'] in task_data:
                    task = task_data[event['task_id']]
                    if kind == 'task.state.delta' and event['seq'] > task['baseline_through_seq']:
                        state = dict(task.get('state') or {})
                        for key in payload.get('remove', []):
                            state.pop(key, None)
                        state.update(payload.get('set', {}))
                        task['state'] = state
                        task['status'] = payload.get('status', task['status'])
                        task['updated_at'] = event['created_at']
                        task['has_delta'] = True
                    elif kind == 'task.updated' and event['seq'] > task['baseline_through_seq'] and not task['has_delta'] and isinstance(payload.get('state'), dict):
                        task['state'] = payload['state']
                        task['status'] = payload.get('status', task['status'])
                        task['updated_at'] = event['created_at']
                    elif kind == 'task.failed' and event['seq'] > task['baseline_through_seq']:
                        if not task['has_delta'] and isinstance(payload.get('state'), dict):
                            task['state'] = payload['state']
                        elif not task['has_delta']:
                            state = dict(task.get('state') or {})
                            state['error'] = payload.get('diagnostic', {}).get('error', 'Task failed')
                            task['state'] = state
                        task['status'] = 'failed'
                        task['updated_at'] = event['created_at']

            # Creation events contain repository roots. Recreate repositories before
            # sessions so a deleted materialized database can be restored.
            for session in session_data.values():
                if session['repository_id']:
                    db.execute('INSERT INTO repositories(repository_id,root_path) VALUES(?,?) ON CONFLICT(repository_id) DO UPDATE SET root_path=excluded.root_path',
                        (session['repository_id'], session['repo_path']))
            for task in task_data.values():
                if task['repository_id'] and task['repository_id'] not in {s['repository_id'] for s in session_data.values()}:
                    db.execute('INSERT OR IGNORE INTO repositories(repository_id,root_path) VALUES(?,?)',
                        (task['repository_id'], task['repo_path']))
            for session_id, session in session_data.items():
                own_events = events_by_session.get(session_id, [])
                revision = max((e['seq'] for e in own_events if e['event_type'] == 'user.message'), default=0)
                updated_at = max((e['created_at'] for e in own_events), default=session['created_at'])
                db.execute('INSERT INTO sessions(session_id,repository_id,created_at,title,updated_at,summary,summary_through_seq,revision,working_state_json) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET repository_id=excluded.repository_id,created_at=excluded.created_at,title=excluded.title,updated_at=excluded.updated_at,summary=excluded.summary,summary_through_seq=excluded.summary_through_seq,revision=excluded.revision,working_state_json=excluded.working_state_json', (
                    session_id, session['repository_id'], session['created_at'], session['title'], updated_at,
                    session['summary'], session['summary_through_seq'], revision,
                    json.dumps(session['working_state'], ensure_ascii=False),
                ))
            for task in task_data.values():
                state = dict(task['state'] or {})
                state.update(task_id=task['task_id'], session_id=task['session_id'], status=task['status'])
                db.execute('INSERT INTO tasks(task_id,session_id,repository_id,repo_path,prompt,status,state_json,config_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET session_id=excluded.session_id,repository_id=excluded.repository_id,repo_path=excluded.repo_path,prompt=excluded.prompt,status=excluded.status,state_json=excluded.state_json,config_json=excluded.config_json,created_at=excluded.created_at,updated_at=excluded.updated_at', (
                    task['task_id'], task['session_id'], task['repository_id'], task['repo_path'], task['prompt'],
                    task['status'], json.dumps(state, ensure_ascii=False), json.dumps(task['config'], ensure_ascii=False),
                    task['created_at'], task['updated_at'],
                ))

            # Conversation messages are a pure event projection. Retain the historical
            # behavior of exposing only the latest final answer per task.
            message_times = dict(db.execute('SELECT message_id,created_at FROM conversation_messages').fetchall())
            db.execute('DELETE FROM conversation_messages')
            latest_final = {}
            for event in events:
                if event['event_type'] == 'assistant.final':
                    latest_final[event.get('task_id')] = event['event_id']
            visible = {'user.message': ('user', 'user'), 'assistant.progress': ('assistant', 'progress'), 'assistant.question': ('assistant', 'question')}
            for event in events:
                kind = event['event_type']
                if kind == 'assistant.final' and latest_final.get(event.get('task_id')) == event['event_id']:
                    role, message_kind = 'assistant', 'final'
                else:
                    if kind not in visible:
                        continue
                    role, message_kind = visible[kind]
                message = ConversationMessage(
                    message_id=event['event_id'], session_id=event['session_id'], task_id=event.get('task_id'),
                    role=role, kind=message_kind, content=str(event['payload'].get('content', '')),
                    seq=event['seq'], created_at=message_times.get(event['event_id'], event['created_at']),
                )
                db.execute('INSERT INTO conversation_messages VALUES(?,?,?,?,?,?,?,?)', tuple(message.model_dump().values()))

            db.execute('DELETE FROM message_receipts')
            for event in events:
                payload = event['payload']
                client_id = payload.get('client_message_id')
                linked = event['event_type'] == 'message.receipt.linked'
                source_id = payload.get('event_id') if linked else event['event_id']
                if client_id and source_id:
                    source = db.execute("SELECT task_id FROM events WHERE event_id=? AND session_id=? AND event_type IN ('user.message','conversation.message_linked')", (source_id, event['session_id'])).fetchone()
                    if source:
                        db.execute('INSERT OR IGNORE INTO message_receipts(session_id,client_message_id,event_id,task_id) VALUES(?,?,?,?)',
                            (event['session_id'], client_id, source_id, event['task_id'] if linked else source['task_id']))

            # Preserve old completed tasks whose final answer predates assistant.final events.
            for task in task_data.values():
                state = task.get('state') or {}
                if task['status'] == 'completed' and state.get('final_answer') and task['task_id'] not in latest_final:
                    seq = max((e['seq'] for e in events if e.get('task_id') == task['task_id']), default=0)
                    message = ConversationMessage(
                        message_id='legacy-final:' + task['task_id'], session_id=task['session_id'],
                        task_id=task['task_id'], role='assistant', kind='final',
                        content=str(state['final_answer']), seq=seq, created_at=task['updated_at'],
                    )
                    db.execute('INSERT INTO conversation_messages VALUES(?,?,?,?,?,?,?,?)', tuple(message.model_dump().values()))

            return {'sessions': len(session_data), 'tasks': len(task_data), 'events': len(events)}

    def replay_task_state(self, task_id, fallback=None):
        """Return the latest event-derived task snapshot without replaying tools."""
        state = {}
        status = state.get('status')
        has_delta = False
        baseline_through_seq = -1
        with self._lock:
            rows = self.connection.execute('SELECT * FROM events WHERE task_id=? ORDER BY seq', (task_id,)).fetchall()
        events = [decode(row) for row in rows]
        has_creation = any(event['event_type'] == 'task.created' for event in events)
        if not has_creation:
            state = dict(fallback or {})
            status = state.get('status')
        for event in events:
            payload = event['payload']
            if event['event_type'] == 'task.created' and not state:
                state = dict(payload.get('state') or {})
                status = payload.get('status', 'created')
                baseline_through_seq = payload.get('legacy_baseline_through_seq', -1)
            elif event['event_type'] == 'task.state.delta' and event['seq'] > baseline_through_seq:
                for key in payload.get('remove', []):
                    state.pop(key, None)
                state.update(payload.get('set', {}))
                status = payload.get('status', status)
                has_delta = True
            elif event['event_type'] == 'task.updated' and event['seq'] > baseline_through_seq and not has_delta and isinstance(payload.get('state'), dict):
                state = dict(payload['state'])
                status = payload.get('status', status)
            elif event['event_type'] == 'task.failed' and event['seq'] > baseline_through_seq:
                if not has_delta and isinstance(payload.get('state'), dict):
                    state = dict(payload['state'])
                elif not has_delta:
                    state['error'] = payload.get('diagnostic', {}).get('error', 'Task failed')
                status = 'failed'
        if status:
            state['status'] = status
        state['task_id'] = task_id
        return state

    def save_artifact(self, content, kind='text'):
        data=content.encode() if isinstance(content,str) else content
        artifact_id=str(uuid.uuid4()); path=self.artifacts_dir / artifact_id
        temporary=path.with_suffix('.tmp')
        with temporary.open('wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        temporary.replace(path)
        with self.transaction() as db:
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)',(artifact_id,str(path.resolve()),hashlib.sha256(data).hexdigest(),len(data),kind))
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id):
        with self._lock:
            result=decode(self.connection.execute('SELECT * FROM artifacts WHERE artifact_id=?',(artifact_id,)).fetchone())
        if result is None: raise KeyError(artifact_id)
        data=Path(result['storage_path']).read_bytes()
        if hashlib.sha256(data).hexdigest()!=result['sha256']: raise ValueError('Artifact integrity check failed')
        result['path']=result['storage_path']
        return result

    def save_checkpoint(self, task_id, state):
        with self.transaction() as db:
            seq=db.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE task_id=? AND seq>0',(task_id,)).fetchone()[0]
            db.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?)',(str(uuid.uuid4()),task_id,seq,json.dumps(state),now()))
        return self.latest_checkpoint(task_id)

    def latest_checkpoint(self, task_id):
        with self._lock:
            return decode(self.connection.execute('SELECT * FROM checkpoints WHERE task_id=? ORDER BY last_event_seq DESC,created_at DESC LIMIT 1',(task_id,)).fetchone())

    def close(self):
        with self._lock: self.connection.close()
