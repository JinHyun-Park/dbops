-- v10: Custom alert conditions — compound AND/OR over multiple metric
-- thresholds, each with its own evaluation window and aggregator. The legacy
-- single-threshold columns (metric_type / comparison / threshold) remain in
-- place so existing rules keep working unchanged; the evaluator prefers
-- `conditions` JSONB when present.
--
-- DSL shape (v10 — flat, no nesting):
--   {
--     "logic": "and" | "or",
--     "operands": [
--       {
--         "metric_type": "cpu",
--         "comparison": ">", "threshold": 80,
--         "window_minutes": 10,
--         "agg": "max" | "avg" | "min" | "last"
--       }, ...
--     ]
--   }
--
-- Constraints:
--  - `logic` defaults to "and" when missing
--  - `agg` defaults to "max" (matches v1 single-threshold semantics)
--  - `window_minutes` defaults to 10 (matches v1 fixed window)

ALTER TABLE alert_rules
  ADD COLUMN IF NOT EXISTS conditions JSONB;

-- Compound rules don't need the legacy single-threshold columns. We relax
-- the NOT NULL constraints so future rules can store NULL there when the
-- DSL is the source of truth. Existing rows are untouched.
ALTER TABLE alert_rules ALTER COLUMN metric_type DROP NOT NULL;
ALTER TABLE alert_rules ALTER COLUMN comparison DROP NOT NULL;
ALTER TABLE alert_rules ALTER COLUMN threshold DROP NOT NULL;

-- Quick lookup of rules that use the compound DSL (vs legacy single
-- threshold). Used by the alert evaluator and admin queries.
CREATE INDEX IF NOT EXISTS idx_alert_rules_has_conditions
  ON alert_rules ((conditions IS NOT NULL));
