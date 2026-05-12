-- v6: Maintenance Health findings.
--
-- The dashboard panel is "findings-driven" — instead of multiple specialty
-- panels (txid age, dead tuples, missing extensions, misconfigured logging
-- params), the collector emits one row per detected issue. Front-end ranks
-- by severity and surfaces an AI-explain action per row.

CREATE TABLE IF NOT EXISTS cluster_health_findings (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(255) NOT NULL,
    snapshot_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    check_type VARCHAR(64) NOT NULL,        -- txid_age | dead_tuples | table_bloat | index_unused | vacuum_overdue | extension_missing | setting_misconfigured
    severity VARCHAR(16) NOT NULL,          -- critical | warning | info
    subject VARCHAR(512) NOT NULL,          -- "public.orders" | "log_checkpoints" | "pg_repack"
    value_str TEXT,                         -- observed value, human-readable
    threshold_str TEXT,                     -- recommended threshold
    recommendation TEXT,                    -- one-line action sentence
    details JSONB                           -- arbitrary extra context for the AI explain step
);

CREATE INDEX IF NOT EXISTS idx_findings_cluster_snapshot
    ON cluster_health_findings (cluster_id, snapshot_time DESC);
CREATE INDEX IF NOT EXISTS idx_findings_cluster_severity
    ON cluster_health_findings (cluster_id, snapshot_time DESC, severity);
