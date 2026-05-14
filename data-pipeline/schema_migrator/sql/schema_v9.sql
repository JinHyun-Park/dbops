-- v9: extend cluster_meta with Serverless v2 ACU configuration so the cost
-- collector can detect over-provisioned max ACU ceilings.
--
-- Aurora Serverless v2 exposes ServerlessV2ScalingConfiguration on
-- DescribeDBClusters (MinCapacity / MaxCapacity, in ACUs). DescribeDBClusters
-- also returns the `EngineMode` field — "serverless" for Serverless v2,
-- otherwise "provisioned". We persist these so cost_check can recommend
-- min/max ACU adjustments without re-querying RDS on every cycle.

ALTER TABLE cluster_meta
  ADD COLUMN IF NOT EXISTS engine_mode VARCHAR(20),
  ADD COLUMN IF NOT EXISTS serverlessv2_min_acu DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS serverlessv2_max_acu DOUBLE PRECISION;

-- Cache for Cost Explorer Savings Plan / RI recommendations. CE charges
-- $0.01 per request so we don't want to call it on every 5-min ETL cycle.
-- One row per (cluster_id, recommendation_type) — overwritten daily.
CREATE TABLE IF NOT EXISTS cost_recommendations_cache (
    cluster_id VARCHAR(255) NOT NULL,
    recommendation_type VARCHAR(64) NOT NULL,
    snapshot_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    estimated_monthly_savings_usd DOUBLE PRECISION,
    recommended_action TEXT,
    details JSONB,
    PRIMARY KEY (cluster_id, recommendation_type)
);
CREATE INDEX IF NOT EXISTS idx_cost_recs_freshness
  ON cost_recommendations_cache (snapshot_time DESC);
