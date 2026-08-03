"""get_performance_summary: four KPIs, each reported with how many samples backed it.

WHY THE SAMPLE COUNTS ARE NOT OPTIONAL
--------------------------------------
The four KPIs are scalar subqueries, so with no rows behind them the response was

    {"kpis": {"avg_aas": null, "max_aas": null, "slow_count": 0,
              "peak_connections": null}}

and nothing in it distinguished "this cluster is idle" from "nothing was collected".
Measured 2026-08-02 across 9 real clusters: 6 of 9 returned all-null KPIs, and
`slow_count` was 0 even on clusters whose `get_slow_queries` returned rows.

`slow_count` is the sharpest case. It reads `query_stats`, which has no producer at
all for DynamoDB and ElastiCache (CAPABILITIES query_stats is False for both), so 0
there does not mean "no slow queries", it means the question cannot be asked. A
count of 0 samples says that; a bare `slow_count: 0` claims the opposite.

So every KPI ships with the number of rows it was computed from. A null KPI with a
positive sample count is a real "no value in this window"; a null KPI with 0 samples
is an absent producer or a collection gap, and the caller can tell which.
"""

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.metric_filters import CLUSTER_LEVEL_ONLY


def get_performance_summary_impl(
    cache: CacheClient,
    cluster_id: str,
    hours: int = 24,
) -> dict:
    window = "ts > NOW() - (:hours || ' hours')::interval"
    qs_window = "snapshot_time > NOW() - (:hours || ' hours')::interval"
    sql = f"""
        SELECT
            (SELECT AVG(value) FROM metric_snapshots WHERE cluster_id = :cluster_id AND metric_type = 'aas' AND {window} {CLUSTER_LEVEL_ONLY}) as avg_aas,
            (SELECT MAX(value) FROM metric_snapshots WHERE cluster_id = :cluster_id AND metric_type = 'aas' AND {window} {CLUSTER_LEVEL_ONLY}) as max_aas,
            (SELECT COUNT(DISTINCT query_hash) FROM query_stats WHERE cluster_id = :cluster_id AND {qs_window} AND mean_time_ms >= 1000) as slow_count,
            (SELECT MAX(value) FROM metric_snapshots WHERE cluster_id = :cluster_id AND metric_type = 'db_connections' AND {window} {CLUSTER_LEVEL_ONLY}) as peak_connections,
            -- Sample counts for the SAME predicates as the aggregates above. Without
            -- these a null KPI is unreadable: idle cluster or no collection?
            (SELECT COUNT(*) FROM metric_snapshots WHERE cluster_id = :cluster_id AND metric_type = 'aas' AND {window} {CLUSTER_LEVEL_ONLY}) as aas_samples,
            (SELECT COUNT(*) FROM metric_snapshots WHERE cluster_id = :cluster_id AND metric_type = 'db_connections' AND {window} {CLUSTER_LEVEL_ONLY}) as connection_samples,
            (SELECT COUNT(*) FROM query_stats WHERE cluster_id = :cluster_id AND {qs_window}) as query_stats_rows
    """
    params = {"cluster_id": cluster_id, "hours": hours}
    result = cache.execute(sql, params)
    row = dict(result.rows[0]) if result.rows else {}

    def _int(key):
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    samples = {
        "aas": _int("aas_samples"),
        "db_connections": _int("connection_samples"),
        "query_stats": _int("query_stats_rows"),
    }
    kpis = {k: row.get(k) for k in ("avg_aas", "max_aas", "slow_count", "peak_connections")}

    # Name the KPIs whose backing rows are absent, so a zero or null is not read as
    # a measurement. `slow_count` is listed when query_stats has NO rows at all in
    # the window: DynamoDB and ElastiCache have no query_stats producer, so 0 there
    # is "cannot be asked", not "none found".
    unbacked = []
    if not samples["aas"]:
        unbacked += ["avg_aas", "max_aas"]
    if not samples["db_connections"]:
        unbacked.append("peak_connections")
    if not samples["query_stats"]:
        unbacked.append("slow_count")

    out = {
        "cluster_id": cluster_id,
        "period_hours": hours,
        "kpis": kpis,
        "samples": samples,
        "unbacked_kpis": unbacked,
    }
    if unbacked:
        out["note"] = (
            f"표본이 없는 KPI: {', '.join(unbacked)}. 이 값들은 측정치가 아니라 "
            "데이터 부재입니다(수집 미시작·수집 중단, 또는 이 엔진에 해당 생산자가 "
            "아예 없음). slow_count는 query_stats 행이 0일 때 '느린 쿼리 없음'이 "
            "아니라 '물을 수 없음'을 뜻합니다(DynamoDB·ElastiCache에는 query_stats "
            "생산자가 없습니다)."
        )
    return out
