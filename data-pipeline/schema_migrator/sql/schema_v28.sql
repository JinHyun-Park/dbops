-- v28: failure-scenario demo runs.
--
-- WHAT A RUN IS. A scenario writes a burst of signal rows into the cache tables
-- diagnose_root_cause already reads (metric_snapshots, event_log, blocking_locks,
-- query_stats, schema_snapshots), then enqueues an auto-RCA anchored on the burst.
-- The ranking, the narrative and the recommendations are the real production path;
-- only the symptom is synthetic. Nothing touches a target database: the registered
-- clusters are read-only fixtures, and a demo that could reboot one is not a demo.
--
-- WHY THE TABLE EXISTS, given the injected rows are themselves a record.
--   * ONE AT A TIME. Two concurrent runs interleave their signals inside the same
--     RCA window, and the resulting report explains a compound incident that no
--     single button caused. The guard needs a row to look at that exists BEFORE
--     the signals are written.
--   * CLEANUP NEEDS A MANIFEST. Injected rows must be removable without touching
--     collected ones. `injected_json` records exactly what was written and where,
--     so the purge is an explicit delete of known rows rather than a
--     pattern-match against real data.
--   * THE REPORT NEEDS AN ANCHOR. `task_id` ties the run to its agent-tasks row,
--     so the UI can show a scenario and the RCA it produced side by side.
--
-- STATUS. `running` from insert until the injection finishes, then `injected`.
-- `resolved` once its rows are purged. There is no `failed`: a run whose
-- injection raises leaves its row at `running`, and the next run's staleness
-- check (older than SCENARIO_LOCK_MINUTES) is what releases the lock. A status
-- the writer sets on its own failure path is a status you cannot trust after a
-- Lambda timeout.
CREATE TABLE IF NOT EXISTS scenario_runs (
    id            BIGSERIAL   PRIMARY KEY,
    scenario_id   VARCHAR(64) NOT NULL,
    cluster_id    VARCHAR(255) NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    anchor_at     TIMESTAMPTZ NOT NULL,
    status        VARCHAR(20) NOT NULL DEFAULT 'running',
    task_id       VARCHAR(128),
    requested_by  VARCHAR(255),
    injected_json JSONB,
    resolved_at   TIMESTAMPTZ
);

-- The concurrency guard reads the newest unresolved run, and the history panel
-- reads the newest N. Both are served by one descending index on started_at.
CREATE INDEX IF NOT EXISTS ix_scenario_runs_started
  ON scenario_runs (started_at DESC);
