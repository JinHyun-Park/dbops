-- High-resolution active-session samples (~5s "near-ASH"), separate from the
-- 5-min ETL. Far higher volume, so it gets its OWN retention (pruned to ~7d by
-- the sampler) instead of riding metric_snapshots, which has no purge and grows
-- unbounded. One row per sample per cluster: the active-session count plus the
-- single dominant wait (full per-wait breakdown at 5s is the upgrade path).
--
-- ponytail: plain table + (cluster_id, ts) index, not RANGE-partitioned like
-- metric_snapshots — at 7d the row count is small (~17k/day/cluster) and a daily
-- DELETE is simpler than partition management. Partition by ts if a large fleet
-- ever makes the prune DELETE expensive.
CREATE TABLE IF NOT EXISTS active_session_samples (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(255) NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    active_sessions INTEGER NOT NULL,
    top_wait VARCHAR(160),
    top_wait_count INTEGER
);

CREATE INDEX IF NOT EXISTS idx_active_session_samples_lookup
    ON active_session_samples (cluster_id, ts);
