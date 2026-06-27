-- v20: support cheap time-range retention purge on metric_snapshots.
-- The table is PARTITION BY RANGE (ts) but only the DEFAULT partition is ever
-- created (no time partitions), so retention is a periodic DELETE WHERE ts <
-- cutoff (run by the ETL collector), not DROP PARTITION. A BRIN index on ts turns
-- that DELETE into an instant block-range check instead of a full seq scan: BRIN
-- fits because rows are inserted in ts order (high physical correlation) and
-- costs almost nothing to maintain on this high-write table.
-- ponytail: BRIN + DELETE. Switch to native RANGE partitions (drop old ones) only
-- if the fleet grows enough that the DELETE/autovacuum churn actually matters.
CREATE INDEX IF NOT EXISTS brin_metric_snapshots_ts
  ON metric_snapshots USING brin (ts);
