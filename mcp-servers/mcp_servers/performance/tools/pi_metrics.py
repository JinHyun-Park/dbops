"""get_pi_metrics: raw Performance Insights time series out of the metric cache.

BOUNDED BY DEFAULT, AND IT SAYS WHAT IT APPLIED
-----------------------------------------------
This tool used to issue `SELECT * FROM metric_snapshots WHERE cluster_id = ...
ORDER BY ts ASC` with no LIMIT and no default window, i.e. all history for the
metric. The RDS Data API caps a response at 1 MB, so the call failed on precisely
the clusters that HAVE Performance Insights data and returned a clean empty on the
ones that do not.

Measured 2026-08-02 by invoking all 64 tools against 9 real clusters: this tool
errored on the three PI-enabled clusters (Aurora PG provisioned, Aurora MySQL, RDS
SQL Server) and looked fine everywhere else, which reads as "PI is not enabled on
those" and is the exact opposite of the truth. Every sibling reader bounds itself
(get_slow_queries and get_top_queries both pass a limit); this one did not.

The window and limit that were actually applied are returned, because a truncated
series that does not say it was truncated is the same trap in a different shape:
the caller would read the clipped set as the whole story.
"""

from mcp_servers.shared.cache_client import CacheClient

# At the ~5s ASH sampling rate a 6h window is far more than 1000 rows, so the LIMIT
# is what really bounds the response; the window keeps the scan off the partition.
_DEFAULT_HOURS = 6
_DEFAULT_LIMIT = 1000
_MAX_LIMIT = 5000


def get_pi_metrics_impl(
    cache: CacheClient,
    cluster_id: str,
    metric_type: str = "aas",
    start_time: str = None,
    end_time: str = None,
    hours: int = _DEFAULT_HOURS,
    limit: int = _DEFAULT_LIMIT,
) -> dict:
    try:
        limit = max(1, min(int(limit), _MAX_LIMIT))
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    try:
        hours = max(1, min(int(hours), 24 * 30))
    except (TypeError, ValueError):
        hours = _DEFAULT_HOURS

    # An explicit start_time wins; otherwise bound the scan to a relative window.
    # `_build_query` only emits a time predicate when start_time is set, so without
    # this branch the query stays unbounded.
    relative_window = not start_time
    extra = ["metric_type = :metric_type"] if metric_type else []
    if relative_window:
        extra.append("ts >= NOW() - (:hours || ' hours')::interval")

    sql, params = cache._build_query(
        table="metric_snapshots",
        cluster_id=cluster_id,
        time_column="ts",
        start_time=start_time,
        end_time=end_time,
        extra_where=" AND ".join(extra) if extra else None,
        # Newest first so a truncated read keeps the RECENT end, which is the half
        # an operator is asking about. Re-sorted ascending below for the caller.
        order_by="ts DESC",
        limit=limit,
    )
    if metric_type:
        params["metric_type"] = metric_type
    if relative_window:
        params["hours"] = hours

    result = cache.execute(sql, params)
    rows = sorted(result.rows, key=lambda r: str(r.get("ts") or ""))
    truncated = result.row_count >= limit
    return {
        "cluster_id": cluster_id,
        "metric_type": metric_type,
        "data_points": rows,
        "count": result.row_count,
        # Provenance, so an empty or clipped series cannot be misread as the whole
        # picture. `window` names what was scanned; `truncated` says the limit bit.
        "window": (
            {"hours": hours} if relative_window
            else {"start_time": start_time, "end_time": end_time}
        ),
        "limit": limit,
        "truncated": truncated,
        "note": (
            f"최근 {limit}개 표본만 반환했습니다 (limit 적용). 더 필요하면 limit을 "
            "올리거나 start_time/end_time으로 창을 좁히세요."
            if truncated else None
        ),
    }
