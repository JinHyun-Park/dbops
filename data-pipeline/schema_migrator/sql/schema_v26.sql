-- v26: schema_snapshots: the table three shipped readers already query and
-- that no producer ever created (get_schema_diff, get_schema_history,
-- diagnose_root_cause's highest-weight schema_change signal).
--
-- Row grain is per (cluster, SCHEMA), not per table: tables_json holds the whole
-- {table_name: [col, ...]} map for one schema in a single jsonb blob, which is
-- what schema_diff's PARTITION BY schema_name and _parse_tables consume. A
-- typical cluster is 1-3 rows per snapshot no matter how many tables it has.
--
-- Written STORE-ON-CHANGE, not every collection cycle: both change-readers
-- already filter out rows whose diff_from_previous_json is empty, and an
-- every-run writer would make the implicit latest-vs-second-latest diff always
-- compare two identical 5-minutes-apart snapshots and report "no changes".
-- So rows/day per cluster equals real DDL deploys, normally 0.
--
-- tables_json is jsonb, not text: the readers filter diff_from_previous_json
-- with both `!= '{}'` (implicit cast to jsonb) and `::text NOT IN ('{}','')`,
-- and jsonb satisfies both. The Data API hands a jsonb column back as a string,
-- which parse_tables already unwraps.
--
-- NOTE: there is no database/catalog column. A multi-database engine has to
-- fold "database" into schema_name, because schema_name is the JOIN key
-- schema_diff matches A against B on.

CREATE TABLE IF NOT EXISTS schema_snapshots (
    cluster_id              TEXT        NOT NULL,
    snapshot_time           TIMESTAMPTZ NOT NULL,
    schema_name             TEXT        NOT NULL,
    tables_json             JSONB       NOT NULL,
    diff_from_previous_json JSONB,
    UNIQUE (cluster_id, schema_name, snapshot_time)
);

-- Range readers: get_schema_history and diagnose_root_cause both scan
-- (cluster_id, snapshot_time half-open window) newest-first.
CREATE INDEX IF NOT EXISTS idx_schema_snapshots_cluster_time
    ON schema_snapshots (cluster_id, snapshot_time DESC);

-- Retention purge (etl_collector tail block, 90 days), same argument as
-- brin_metric_snapshots_ts in v20: rows are inserted in snapshot_time order so
-- physical correlation is high, and BRIN turns the purge DELETE into a
-- block-range check for almost no maintenance cost. Under store-on-change the
-- purge normally matches zero rows.
CREATE INDEX IF NOT EXISTS brin_schema_snapshots_time
    ON schema_snapshots USING brin (snapshot_time);
