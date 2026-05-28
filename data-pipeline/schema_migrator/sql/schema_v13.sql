-- v13: Saved SQL queries. The Query Lab page had a localStorage history
-- of recent EXPLAIN runs but no way to bookmark a tuned query for later.
-- This table is the durable scratchpad — DBAs save canonical queries
-- (slow-query repros, capacity probes, audit selects) once and pull them
-- back up across browsers and devices.
--
-- The body is plain SQL text; tags + cluster_id let a future feature
-- (e.g. /ask) match the saved library against the current cluster.

CREATE TABLE IF NOT EXISTS saved_queries (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(255),         -- nullable: a query can be cluster-agnostic
    title TEXT NOT NULL,
    description TEXT,                -- one-line context shown in the list view
    sql_text TEXT NOT NULL,          -- the query body
    tags TEXT[] DEFAULT '{}',        -- e.g. ['capacity', 'audit', 'idx-recommend']
    created_by VARCHAR(255),         -- Cognito sub / username
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_saved_queries_user ON saved_queries (created_by, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_saved_queries_cluster ON saved_queries (cluster_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_saved_queries_tags ON saved_queries USING GIN (tags);
