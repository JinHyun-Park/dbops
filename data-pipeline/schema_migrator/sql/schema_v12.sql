-- v12: AI-generated runbooks. When the agent diagnoses an incident or an
-- anomaly and recommends a remediation, the DBA can stamp that exchange as
-- a reusable runbook so the next time the same pattern recurs the team can
-- consult the saved playbook instead of re-asking the agent.
--
-- The runbook body is stored as Markdown (the agent's natural output
-- format). Tags + cluster_id let us match a future incident against past
-- runbooks via the find_similar_incidents MCP tool.

CREATE TABLE IF NOT EXISTS runbooks (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(255),         -- nullable: a runbook can be cluster-agnostic
    title TEXT NOT NULL,
    summary_md TEXT,                 -- one-line headline rendered in lists
    body_md TEXT NOT NULL,           -- the full diagnosis + remediation
    tags TEXT[] DEFAULT '{}',        -- e.g. ['high-cpu', 'autovacuum', 'idle-in-tx']
    source TEXT,                     -- 'chat' | 'anomaly' | 'manual'
    source_ref VARCHAR(255),         -- chat session id, anomaly row id, etc.
    created_by VARCHAR(255),         -- Cognito username
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_runbooks_cluster ON runbooks (cluster_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runbooks_tags ON runbooks USING GIN (tags);
