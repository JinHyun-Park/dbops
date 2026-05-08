CREATE TABLE IF NOT EXISTS cluster_meta (
    cluster_id VARCHAR(255) PRIMARY KEY,
    account_id VARCHAR(12) NOT NULL,
    region VARCHAR(20) NOT NULL,
    engine VARCHAR(20) NOT NULL,
    engine_version VARCHAR(20),
    instance_class VARCHAR(50),
    status VARCHAR(20),
    endpoint TEXT,
    reader_endpoint TEXT,
    storage_size_gb DECIMAL(10,2),
    max_connections INT,
    spoke_role_arn TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id BIGSERIAL,
    cluster_id VARCHAR(255) NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    metric_type VARCHAR(50) NOT NULL,
    value DOUBLE PRECISION,
    dimensions JSONB,
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE IF NOT EXISTS query_stats (
    id BIGSERIAL,
    cluster_id VARCHAR(255) NOT NULL,
    snapshot_time TIMESTAMPTZ NOT NULL,
    query_hash VARCHAR(64) NOT NULL,
    query_text TEXT,
    calls BIGINT,
    total_time_ms DOUBLE PRECISION,
    mean_time_ms DOUBLE PRECISION,
    rows_returned BIGINT,
    shared_blks_hit BIGINT,
    shared_blks_read BIGINT,
    PRIMARY KEY (id, snapshot_time)
) PARTITION BY RANGE (snapshot_time);

CREATE TABLE IF NOT EXISTS slow_queries (
    id BIGSERIAL,
    cluster_id VARCHAR(255) NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    query_text TEXT,
    execution_time_ms DOUBLE PRECISION,
    lock_time_ms DOUBLE PRECISION,
    rows_examined BIGINT,
    rows_sent BIGINT,
    db_name VARCHAR(255),
    user_name VARCHAR(255),
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX idx_metric_snapshots_lookup ON metric_snapshots (cluster_id, metric_type, ts);
CREATE INDEX idx_query_stats_lookup ON query_stats (cluster_id, snapshot_time);
CREATE INDEX idx_slow_queries_lookup ON slow_queries (cluster_id, ts);
