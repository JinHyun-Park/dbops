-- schema_v17 — scheduled agent tasks (Agent Tasks, increment 4)
--
-- Recurring agent work definitions, mirrored on the alert_rules pattern. The
-- task_scheduler Lambda (EventBridge) reads enabled rows, decides which are due
-- by interval_kind vs last_run_at, and enqueues a pending row into the
-- agent-tasks DynamoDB table — the same single processing path the
-- task_worker drains. See docs/superpowers/specs/2026-06-18-agent-tasks-design.md.

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(255) NOT NULL,
    -- what to run when due. Today: scheduled_report (top-slow-query + health
    -- summary). Kept as a column so future scheduled kinds slot in.
    kind VARCHAR(50) NOT NULL DEFAULT 'scheduled_report',
    -- coarse cadence; the scheduler compares NOW() - last_run_at against this.
    -- hourly | daily | weekly (no cron expressions in this iteration).
    interval_kind VARCHAR(20) NOT NULL,
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_enabled
    ON scheduled_tasks (enabled, interval_kind);
