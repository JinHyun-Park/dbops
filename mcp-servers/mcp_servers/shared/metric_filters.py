"""metric_snapshots dimension filters (canonical pure module).

`metric_snapshots` stores the SAME `metric_type` at several dimensionalities:

  * cluster/table level   -> dimensions IS NULL or '{}'
  * per instance          -> {"instance": "...", "role": "..."}   (cw_collector)
  * per PI wait event     -> {"db.wait_event.name": ..., "db.wait_event.type": ...}
                             (pi_collector: `aas` is written once per wait event
                              PLUS one '{}' total row)
  * per DynamoDB GSI      -> {"gsi": "..."}                       (dynamodb_cw_collector)
  * per SQL Server wait   -> {"wait_type": "..."}                 (mssql_waits)

Aggregating a cluster-level number without a dimension filter silently mixes a
total with its own fractions. No error, just a wrong number. This has bitten the
project three times (Instance Compare 2026-06-22, `forecast_capacity` in E-0,
and the E1-1 audit).

Use CLUSTER_LEVEL_ONLY for any cluster-level scalar/aggregate/regression.
EXCLUDE_PER_INSTANCE is ONLY for readers that deliberately return the
dimensioned detail rows (wait-event stacked chart, per-GSI panel) and merely
need the per-instance duplicates dropped.

No shared Lambda layer spans api/ · data-pipeline/ · mcp-servers/, so the
constants are duplicated VERBATIM in:
  - api/dashboard/metric_filters.py
Other packages (api/simulation, data-pipeline/*) inline the same literal text,
matching what already existed there; tests/unit/test_metric_filters.py greps the
repo so no site can drift back to the weak form.
"""

# Strict cluster-level filter. Leading AND: append to an existing WHERE.
#
# Do NOT weaken this to `NOT jsonb_exists(dimensions, 'instance')`: PI
# wait-event rows and DynamoDB GSI rows carry no 'instance' key, so they survive
# that form and pollute the aggregate. Two reviewers split on this in E-0; strict
# is the correct form.
CLUSTER_LEVEL_ONLY = "AND (dimensions IS NULL OR dimensions::text = '{}')"

# Keeps every dimensioned detail row EXCEPT per-instance ones. Only for readers
# that intentionally break a metric down by wait event / GSI. Never for a
# cluster-level number.
#
# `jsonb_exists(col, key)` instead of the `?` operator because the RDS Data API
# rejects `?` as a positional-parameter character.
EXCLUDE_PER_INSTANCE = "AND (dimensions IS NULL OR NOT jsonb_exists(dimensions, 'instance'))"


def cluster_level_only(alias: str) -> str:
    """CLUSTER_LEVEL_ONLY qualified with a table alias, for self-joins /
    correlated subqueries (e.g. `cluster_level_only("pp")`)."""
    return f"AND ({alias}.dimensions IS NULL OR {alias}.dimensions::text = '{{}}')"
