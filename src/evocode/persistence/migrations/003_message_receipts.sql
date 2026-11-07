CREATE TABLE IF NOT EXISTS message_receipts(
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  client_message_id TEXT NOT NULL,
  event_id TEXT NOT NULL REFERENCES events(event_id),
  task_id TEXT,
  PRIMARY KEY(session_id, client_message_id)
);
