-- v8: Seasonal baselines for anomaly detection.
--
-- Current /anomalies endpoint uses a flat 7-day mean+stddev which misses
-- seasonality (e.g. CPU is naturally higher 09:00 KST than 03:00 KST).
-- We bucket history by hour-of-week (0..167) and store median + IQR per
-- bucket. Detection at query time compares the latest value against the
-- bucket the current timestamp falls into — a robust z-score that doesn't
-- false-positive on daily cycles.

CREATE TABLE IF NOT EXISTS metric_baselines (
    cluster_id VARCHAR(255) NOT NULL,
    metric_type VARCHAR(64) NOT NULL,
    hour_of_week INTEGER NOT NULL,   -- 0..167 (dow*24 + hour)
    median DOUBLE PRECISION NOT NULL,
    iqr DOUBLE PRECISION NOT NULL,
    sample_count INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cluster_id, metric_type, hour_of_week)
);

CREATE INDEX IF NOT EXISTS idx_metric_baselines_cluster_metric
    ON metric_baselines (cluster_id, metric_type);
