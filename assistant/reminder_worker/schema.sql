CREATE TABLE IF NOT EXISTS reminders (
  id TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  due_ms INTEGER NOT NULL,
  repeat TEXT NOT NULL DEFAULT 'none',
  tz_offset_min INTEGER NOT NULL DEFAULT 330,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  created_ms INTEGER NOT NULL,
  last_sent_ms INTEGER
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, due_ms);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
