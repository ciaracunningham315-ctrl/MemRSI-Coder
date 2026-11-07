ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';
UPDATE sessions SET title='对话' WHERE title='';
UPDATE sessions SET updated_at=created_at WHERE updated_at='';
