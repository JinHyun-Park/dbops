-- v22: opportunistic query-plan history for "plan flip vs data growth".
-- Every EXPLAIN the agent runs (explain_plan tool) records a STRUCTURAL plan
-- signature (node types + relations/indexes/join types, costs/rows EXCLUDED).
-- Re-EXPLAINing the same query later reveals whether the PLAN changed (a flip —
-- index/join switch) vs the same plan just getting slower (data growth). Keyed
-- by a LITERAL-normalized SQL hash (string + numeric literals stripped) so the
-- same logical query matches across runs regardless of parameter values.
-- ponytail: opportunistic capture from the EXPLAIN the tool already runs (zero
-- extra target load, version-agnostic). Fully-automatic continuous plan capture
-- for EVERY top query needs PG16 `EXPLAIN (GENERIC_PLAN)` to plan normalized $1
-- queries — Aurora is on PG15 here, so that broader version stays deferred.
CREATE TABLE IF NOT EXISTS query_plan_history (
    id BIGSERIAL PRIMARY KEY,
    cluster_id VARCHAR(255) NOT NULL,
    query_sig VARCHAR(64) NOT NULL,      -- md5 of the normalized query text
    plan_hash VARCHAR(64) NOT NULL,      -- md5 of the structural plan signature
    plan_summary TEXT,                   -- short node-type breadcrumb for humans
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_query_plan_history_lookup
  ON query_plan_history (cluster_id, query_sig, captured_at DESC);
