-- data-pipeline/schema_migrator/sql/schema_v28.sql
-- v28: APM feature for EC2 Java/Spring Boot apps. Cache-first host/APM metrics
-- plus per-level log COUNTS. Raw log lines are deliberately NOT stored here --
-- they are fetched on demand from CloudWatch at search time (cost/capacity/
-- security). apm_target_meta is a convenience mirror; the source of truth for
-- targets is the DynamoDB apm_targets registry.

CREATE TABLE IF NOT EXISTS apm_target_meta (
  target_id     VARCHAR(255) PRIMARY KEY,
  instance_id   VARCHAR(64),
  region        VARCHAR(32),
  service_name  VARCHAR(255),
  log_groups    JSONB,
  team          VARCHAR(255),
  last_seen_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS apm_metric_snapshots (
  target_id    VARCHAR(255),
  ts           TIMESTAMPTZ,
  metric_type  VARCHAR(64),
  value        DOUBLE PRECISION,
  dimensions   JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_apm_metric_lookup
  ON apm_metric_snapshots (target_id, metric_type, ts);

CREATE TABLE IF NOT EXISTS apm_log_level_counts (
  target_id  VARCHAR(255),
  ts         TIMESTAMPTZ,
  log_group  VARCHAR(512),
  level      VARCHAR(16),
  count      BIGINT,
  UNIQUE (target_id, ts, log_group, level)
);
