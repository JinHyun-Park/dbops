"""capacity_cost — DynamoDB Provisioned↔On-Demand $ what-if (READ-ONLY).

Reads the table's ACTUAL consumed capacity from the cache (metric_snapshots) and
its billing_mode/region from cluster_meta, prices both modes with the REAL AWS
Price List API for the table's region, and returns the monthly cost comparison +
a recommendation. The math lives in the shared dynamodb_cost module (tested once,
shared with the REST mirror); pricing lives in dynamodb_pricing. Both fail soft —
a missing price degrades to partial/fallback, never a fabricated dollar number.

Capacity-unit semantics (must match the findings collector to avoid the
GSI-dimension-mixing trap):
  - consumed_rcu / consumed_wcu are 1-min Sum; per-GSI rows carry
    dimensions={"gsi":..}, so we filter (dimensions IS NULL OR dimensions::text='{}')
    to keep table-level totals only.
  - provisioned_rcu / provisioned_wcu are per-second Average and exist ONLY for
    PROVISIONED tables (used for the current-mode cost).
"""

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.dynamodb_cost import compute_capacity_cost
from mcp_servers.shared.dynamodb_pricing import (
    price_per_million_rru,
    price_per_million_wru,
    price_per_rcu_hour,
    price_per_wcu_hour,
)

_WINDOW_CAP_HOURS = 168  # 7 days


def _fnum(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _gather_consumed(cache: CacheClient, cluster_id: str, window_hours: float) -> dict:
    """Per-side consumed aggregates over the window (table-level only). p99 is of
    the per-minute Sum series; datapoints counts the consumed_rcu rows."""
    sql = (
        "SELECT "
        "  COUNT(*) FILTER (WHERE metric_type = 'consumed_rcu') AS datapoints, "
        "  COALESCE(SUM(value) FILTER (WHERE metric_type = 'consumed_rcu'), 0) AS sum_rcu, "
        "  COALESCE(SUM(value) FILTER (WHERE metric_type = 'consumed_wcu'), 0) AS sum_wcu, "
        "  COALESCE(percentile_cont(0.99) WITHIN GROUP ("
        "    ORDER BY value) FILTER (WHERE metric_type = 'consumed_rcu'), 0) AS p99_rcu, "
        "  COALESCE(percentile_cont(0.99) WITHIN GROUP ("
        "    ORDER BY value) FILTER (WHERE metric_type = 'consumed_wcu'), 0) AS p99_wcu "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        "  AND metric_type IN ('consumed_rcu', 'consumed_wcu') "
        "  AND ts > NOW() - (:hours || ' hours')::interval "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')"
    )
    rows = cache.execute(sql, {"cid": cluster_id, "hours": str(window_hours)}).rows
    row = rows[0] if rows else {}
    return {
        "datapoints": int(row.get("datapoints") or 0),
        "sum_rcu": _fnum(row.get("sum_rcu")) or 0.0,
        "sum_wcu": _fnum(row.get("sum_wcu")) or 0.0,
        "p99_rcu_per_min": _fnum(row.get("p99_rcu")) or 0.0,
        "p99_wcu_per_min": _fnum(row.get("p99_wcu")) or 0.0,
    }


def _latest_provisioned(cache: CacheClient, cluster_id: str):
    """Latest per-second provisioned_rcu/wcu for a PROVISIONED table, or None."""
    sql = (
        "SELECT metric_type, value FROM metric_snapshots m "
        "WHERE cluster_id = :cid AND metric_type IN ('provisioned_rcu', 'provisioned_wcu') "
        "  AND (dimensions IS NULL OR dimensions::text = '{}') "
        "  AND ts = (SELECT MAX(ts) FROM metric_snapshots "
        "            WHERE cluster_id = m.cluster_id AND metric_type = m.metric_type "
        "              AND (dimensions IS NULL OR dimensions::text = '{}'))"
    )
    rows = cache.execute(sql, {"cid": cluster_id}).rows
    if not rows:
        return None
    out = {}
    for r in rows:
        if r.get("metric_type") == "provisioned_rcu":
            out["rcu"] = _fnum(r.get("value")) or 0.0
        elif r.get("metric_type") == "provisioned_wcu":
            out["wcu"] = _fnum(r.get("value")) or 0.0
    return out or None


def _meta(cache: CacheClient, cluster_id: str):
    """(billing_mode, region, table_class, is_global_table) from cluster_meta.
    billing_mode/table_class are in resource_details JSONB; region is a top-level
    column. Absent table_class defaults to STANDARD (pre-fix ETL rows)."""
    sql = (
        "SELECT region, "
        "resource_details->>'billing_mode' AS billing_mode, "
        "resource_details->>'table_class' AS table_class, "
        "resource_details->'global_table_replicas' AS global_table_replicas "
        "FROM cluster_meta WHERE cluster_id = :cid"
    )
    rows = cache.execute(sql, {"cid": cluster_id}).rows
    if not rows:
        return None, "", "STANDARD", False
    row = rows[0]
    table_class = row.get("table_class") or "STANDARD"
    raw_replicas = row.get("global_table_replicas")
    try:
        import json as _json
        replicas = _json.loads(raw_replicas) if isinstance(raw_replicas, str) else (raw_replicas or [])
    except (TypeError, ValueError):
        replicas = []
    is_global_table = bool(replicas)
    return row.get("billing_mode"), (row.get("region") or ""), table_class, is_global_table


def simulate_dynamodb_capacity_cost_impl(
    cache: CacheClient,
    cluster_id: str,
    headroom: float = 0.70,
    window_hours: float = 168,
    **_ignored,
) -> dict:
    """Provisioned↔On-Demand monthly $ what-if for a DynamoDB table.

    `cluster_id` is the registry PK (ddb-* slug). `headroom` is the target
    utilization for provisioned sizing (default 0.70). `window_hours` caps the
    consumed lookback (default/cap 168h). Extra kwargs from the MCP dispatcher
    are ignored. Never raises a pricing/data error into the caller — degrades to
    partial/fallback/no_data per the honesty contract."""
    try:
        window = float(window_hours)
    except (TypeError, ValueError):
        window = _WINDOW_CAP_HOURS
    window = max(1.0, min(window, _WINDOW_CAP_HOURS))

    billing_mode, region, table_class, is_global_table = _meta(cache, cluster_id)
    consumed = _gather_consumed(cache, cluster_id, window)
    provisioned = (
        _latest_provisioned(cache, cluster_id)
        if billing_mode == "PROVISIONED"
        else None
    )

    prices = {
        "rcu_hr": price_per_rcu_hour(region) if region else None,
        "wcu_hr": price_per_wcu_hour(region) if region else None,
        "m_rru": price_per_million_rru(region) if region else None,
        "m_wru": price_per_million_wru(region) if region else None,
    }

    return compute_capacity_cost(
        cluster_id=cluster_id,
        billing_mode=billing_mode,
        region=region,
        window_hours=window,
        consumed=consumed,
        provisioned=provisioned,
        prices=prices,
        headroom=headroom,
        table_class=table_class,
        is_global_table=is_global_table,
    )
