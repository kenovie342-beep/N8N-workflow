-- D1 schema. All timestamps are epoch milliseconds (UTC).
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS settings (            -- llm / social / github: secrets AES-GCM encrypted
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
  config_json TEXT NOT NULL DEFAULT '{}', secret_enc TEXT NOT NULL DEFAULT '',
  is_default INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_settings_kind ON settings (kind);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, task TEXT NOT NULL, workflow TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'queued',
  priority INTEGER NOT NULL DEFAULT 5, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
  error TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{}',
  dedupe_key TEXT, schedule_id TEXT, executor TEXT NOT NULL DEFAULT 'worker', remote_run_id TEXT NOT NULL DEFAULT '',
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  run_after INTEGER NOT NULL, lease_expires_at INTEGER, started_at INTEGER, finished_at INTEGER,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_jobs_claim ON jobs (status, priority, created_at);
CREATE INDEX IF NOT EXISTS ix_jobs_schedule ON jobs (schedule_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active_dedupe ON jobs (dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('queued','running');

CREATE TABLE IF NOT EXISTS job_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, ts INTEGER NOT NULL, level TEXT NOT NULL DEFAULT 'info', message TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_job_logs_job ON job_logs (job_id);

CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, task TEXT NOT NULL, workflow TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}', kind TEXT NOT NULL DEFAULT 'cron', cron_expr TEXT NOT NULL DEFAULT '',
  run_at INTEGER, interval_seconds INTEGER NOT NULL DEFAULT 0, timezone TEXT NOT NULL DEFAULT 'Africa/Lagos',
  priority INTEGER NOT NULL DEFAULT 5, enabled INTEGER NOT NULL DEFAULT 1, next_run_at INTEGER, last_run_at INTEGER,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_schedules_next ON schedules (enabled, next_run_at);

CREATE TABLE IF NOT EXISTS nonces (nonce TEXT PRIMARY KEY, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rate_limits (k TEXT PRIMARY KEY, window_start INTEGER NOT NULL, hits INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS n8n_state (id INTEGER PRIMARY KEY CHECK (id = 1), last_beat INTEGER, run_id TEXT, tunnel_url TEXT, last_dispatch INTEGER, window_end INTEGER);
INSERT OR IGNORE INTO n8n_state (id) VALUES (1);
