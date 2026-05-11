-- Phase 4+ additions: extend cluster_meta with backup/maintenance + add new feature tables.

ALTER TABLE cluster_meta
  ADD COLUMN IF NOT EXISTS backup_retention_days INTEGER,
  ADD COLUMN IF NOT EXISTS earliest_restorable_time TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS latest_restorable_time TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS preferred_backup_window VARCHAR(50),
  ADD COLUMN IF NOT EXISTS preferred_maintenance_window VARCHAR(50),
  ADD COLUMN IF NOT EXISTS multi_az BOOLEAN,
  ADD COLUMN IF NOT EXISTS deletion_protection BOOLEAN;

-- Required default partitions for the partitioned tables defined in schema.sql
-- (cdk auto-deploy on a brand-new database hits "no partition" errors without these).
CREATE TABLE IF NOT EXISTS metric_snapshots_default PARTITION OF metric_snapshots DEFAULT;
CREATE TABLE IF NOT EXISTS query_stats_default PARTITION OF query_stats DEFAULT;
CREATE TABLE IF NOT EXISTS slow_queries_default PARTITION OF slow_queries DEFAULT;

-- Unique constraint that lets ETL collectors use ON CONFLICT DO NOTHING for idempotency.
CREATE UNIQUE INDEX IF NOT EXISTS uix_metric_snapshots
  ON metric_snapshots (cluster_id, ts, metric_type, md5(COALESCE(dimensions::text, '{}')));

-- pg_stat_user_tables snapshot per ETL run.
CREATE TABLE IF NOT EXISTS table_stats (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(255) NOT NULL,
    snapshot_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    schema_name VARCHAR(255),
    table_name VARCHAR(255),
    n_live_tup BIGINT,
    n_dead_tup BIGINT,
    seq_scan BIGINT,
    idx_scan BIGINT,
    seq_tup_read BIGINT,
    idx_tup_fetch BIGINT,
    last_vacuum TIMESTAMPTZ,
    last_analyze TIMESTAMPTZ,
    total_bytes BIGINT,
    table_bytes BIGINT,
    index_bytes BIGINT
);
CREATE INDEX IF NOT EXISTS idx_table_stats_lookup
  ON table_stats (cluster_id, snapshot_time DESC, schema_name, table_name);

-- pg_stat_activity snapshot of queries running > 5 seconds.
CREATE TABLE IF NOT EXISTS long_running_queries (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(255) NOT NULL,
    snapshot_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pid BIGINT,
    username VARCHAR(255),
    state VARCHAR(50),
    duration_sec DOUBLE PRECISION,
    xact_duration_sec DOUBLE PRECISION,
    query_text TEXT,
    wait_event_type VARCHAR(100),
    wait_event VARCHAR(100),
    client_addr VARCHAR(100)
);
CREATE INDEX IF NOT EXISTS idx_long_running_lookup
  ON long_running_queries (cluster_id, snapshot_time DESC);

-- pg_locks blocking chains.
CREATE TABLE IF NOT EXISTS blocking_locks (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(255) NOT NULL,
    snapshot_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    blocked_pid BIGINT,
    blocked_user VARCHAR(255),
    blocking_pid BIGINT,
    blocking_user VARCHAR(255),
    blocked_query TEXT,
    blocking_query TEXT,
    locktype VARCHAR(50),
    blocked_mode VARCHAR(50),
    blocking_mode VARCHAR(50),
    relation VARCHAR(255),
    blocked_duration_sec DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_blocking_locks_lookup
  ON blocking_locks (cluster_id, snapshot_time DESC);

-- Per-cluster PostgreSQL config (max_connections, shared_buffers, etc.) — upserted by ETL.
CREATE TABLE IF NOT EXISTS cluster_settings (
    cluster_id VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    value TEXT,
    unit VARCHAR(50),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cluster_id, name)
);

-- DBA-defined alert rules evaluated every 5 minutes by alert_evaluator Lambda.
CREATE TABLE IF NOT EXISTS alert_rules (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    metric_type VARCHAR(100) NOT NULL,
    comparison VARCHAR(10) NOT NULL,
    threshold DOUBLE PRECISION NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_triggered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_alert_rules_cluster ON alert_rules (cluster_id, enabled);
