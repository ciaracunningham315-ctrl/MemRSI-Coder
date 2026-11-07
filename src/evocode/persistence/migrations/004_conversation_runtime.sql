ALTER TABLE sessions ADD COLUMN summary TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN summary_through_seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN working_state_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE conversation_messages (
  message_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  task_id TEXT REFERENCES tasks(task_id),
  role TEXT NOT NULL CHECK(role IN ('user','assistant')),
  kind TEXT NOT NULL CHECK(kind IN ('user','progress','question','final')),
  content TEXT NOT NULL,
  seq INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX conversation_messages_session ON conversation_messages(session_id,seq);
CREATE UNIQUE INDEX conversation_one_final ON conversation_messages(task_id) WHERE kind='final';

INSERT INTO conversation_messages
SELECT event_id,session_id,task_id,
       CASE WHEN event_type='user.message' THEN 'user' ELSE 'assistant' END,
       CASE event_type WHEN 'user.message' THEN 'user' WHEN 'assistant.question' THEN 'question' WHEN 'assistant.progress' THEN 'progress' ELSE 'final' END,
       COALESCE(json_extract(payload_json,'$.content'),''),seq,created_at
FROM events e
WHERE event_type IN ('user.message','assistant.final','assistant.question','assistant.progress')
AND (event_type!='assistant.final' OR seq=(SELECT MAX(e2.seq) FROM events e2 WHERE e2.task_id=e.task_id AND e2.event_type='assistant.final'));

-- Older executions published only final_answer in task state. Preserve their
-- actual final text without copying internal coder messages into the chat.
INSERT INTO conversation_messages
SELECT 'legacy-final:'||t.task_id,t.session_id,t.task_id,'assistant','final',
       json_extract(t.state_json,'$.final_answer'),
       (SELECT COALESCE(MAX(seq),0) FROM events WHERE task_id=t.task_id),t.updated_at
FROM tasks t WHERE t.status='completed'
AND length(COALESCE(json_extract(t.state_json,'$.final_answer'),''))>0
AND NOT EXISTS (SELECT 1 FROM conversation_messages m WHERE m.task_id=t.task_id AND m.kind='final');

UPDATE sessions SET revision=(SELECT COALESCE(MAX(seq),0) FROM events WHERE events.session_id=sessions.session_id AND event_type='user.message');
