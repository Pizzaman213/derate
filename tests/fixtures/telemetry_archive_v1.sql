-- The telemetry archive schema at SCHEMA_VERSION 1, frozen from
-- control_plane/telemetry/archive.py at ca4908a^ (the commit before v2).
-- test_telemetry_keys.py migrates a file built from it. Never edit.

CREATE TABLE IF NOT EXISTS samples(
  node_id               TEXT NOT NULL,
  ts                    REAL NOT NULL,
  memory_used           INTEGER,
  memory_total          INTEGER,
  power_w               REAL,
  temp_c                REAL,
  util_pct              REAL,
  gpu_memory_used       INTEGER,
  gpu_process_count     INTEGER,
  host_memory_total     INTEGER,
  host_memory_available INTEGER,
  swap_used             INTEGER,
  PRIMARY KEY(node_id, ts)
) WITHOUT ROWID;

-- One row per attempt, not per client request: a request that failed over
-- produces two, and the pair is the record of the failover having happened.
CREATE TABLE IF NOT EXISTS requests(
  request_id       TEXT NOT NULL,
  attempt_no       INTEGER NOT NULL DEFAULT 0,
  ts               REAL NOT NULL,
  node_id          TEXT,
  served_name      TEXT,
  target_id        TEXT,
  target_kind      TEXT,
  provider_id      TEXT,
  deployment_id    TEXT,
  policy           TEXT,
  strength_source  TEXT,
  attempts         INTEGER,
  retry_reason     TEXT,
  status           INTEGER,
  error_code       TEXT,
  error_class      TEXT,
  prompt_tokens    INTEGER,
  completion_tokens INTEGER,
  tokens           INTEGER,
  tokens_estimated INTEGER,
  cost_usd         REAL,
  ttft_ms          REAL,
  decode_ms        REAL,
  duration_ms      REAL,
  parked_ms        REAL,
  streaming        INTEGER,
  body_bytes       INTEGER,
  admission_code   TEXT,
  kv_bytes         INTEGER,
  PRIMARY KEY(request_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS requests_model_ts ON requests(served_name, ts);

CREATE TABLE IF NOT EXISTS events(
  node_id       TEXT NOT NULL,
  seq           INTEGER NOT NULL,
  ts            REAL NOT NULL,
  source        TEXT,
  type          TEXT,
  deployment_id TEXT,
  served_name   TEXT,
  body          TEXT NOT NULL,
  PRIMARY KEY(node_id, seq)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_type_ts ON events(type, ts);

CREATE TABLE IF NOT EXISTS logs(
  node_id TEXT NOT NULL,
  seq     INTEGER NOT NULL,
  ts      REAL NOT NULL,
  level   TEXT,
  logger  TEXT,
  message TEXT,
  body    TEXT,
  PRIMARY KEY(node_id, seq)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS logs_ts ON logs(ts);
CREATE INDEX IF NOT EXISTS logs_level_ts ON logs(level, ts);

CREATE TABLE IF NOT EXISTS cursors(
  node_id      TEXT PRIMARY KEY,
  last_seq     INTEGER NOT NULL DEFAULT 0,
  last_ship_ts REAL,
  dropped      INTEGER NOT NULL DEFAULT 0,
  head         INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT
);

CREATE TABLE IF NOT EXISTS gaps(
  node_id TEXT NOT NULL,
  from_ts REAL NOT NULL,
  to_ts   REAL NOT NULL,
  reason  TEXT,
  PRIMARY KEY(node_id, from_ts, reason)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rollup_samples(
  step        INTEGER NOT NULL,
  node_id     TEXT NOT NULL,
  bucket      INTEGER NOT NULL,
  n           INTEGER NOT NULL,
  power_w_avg REAL, power_w_max REAL,
  temp_c_avg  REAL, temp_c_max  REAL,
  util_pct_avg REAL, util_pct_max REAL,
  memory_used_avg REAL, memory_used_max INTEGER,
  gpu_memory_used_avg REAL, gpu_memory_used_max INTEGER,
  host_memory_available_min INTEGER,
  swap_used_max INTEGER,
  PRIMARY KEY(step, node_id, bucket)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rollup_requests(
  step        INTEGER NOT NULL,
  bucket      INTEGER NOT NULL,
  served_name TEXT NOT NULL,
  target_id   TEXT NOT NULL,
  n           INTEGER NOT NULL,
  ok          INTEGER NOT NULL,
  failed      INTEGER NOT NULL,
  tokens      INTEGER NOT NULL,
  prompt_tokens INTEGER NOT NULL,
  cost_usd    REAL,
  ttft_hist   BLOB,
  duration_hist BLOB,
  PRIMARY KEY(step, bucket, served_name, target_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
