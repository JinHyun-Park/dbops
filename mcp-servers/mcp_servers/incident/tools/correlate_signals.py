from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.incident_signals import metric_in_clause, resolve_family, timeline_metrics
from mcp_servers.shared.metric_filters import CLUSTER_LEVEL_ONLY


def correlate_signals_impl(
    cache: CacheClient,
    cluster_id: str,
    start_time: str,
    end_time: str,
) -> dict:
    # The metric series a cluster actually has depends on its engine family
    # (Aurora aas/cpu/db_connections vs DocumentDB cpu_utilization vs ElastiCache
    # cache_cpu vs DynamoDB throttle/consumed series). Hardcoding Aurora's three
    # names gave every non-relational engine an events-only timeline that looked
    # like "no metric activity". See shared/incident_signals.py.
    family, family_resolved = resolve_family(cache, cluster_id)
    metrics = timeline_metrics(family)
    in_clause, name_params = metric_in_clause(metrics)
    metrics_sql = f"""
        SELECT ts as event_time, 'metric' as signal_type, metric_type as detail, value::text as value
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id AND ts >= :start_time::timestamptz AND ts < :end_time::timestamptz
        AND metric_type {in_clause}
        {CLUSTER_LEVEL_ONLY}
    """
    events_sql = """
        SELECT event_time, 'event' as signal_type, event_type as detail, message as value
        FROM event_log
        WHERE cluster_id = :cluster_id AND event_time >= :start_time::timestamptz AND event_time < :end_time::timestamptz
    """
    combined_sql = f"({metrics_sql}) UNION ALL ({events_sql}) ORDER BY event_time"
    params = {
        "cluster_id": cluster_id,
        "start_time": start_time,
        "end_time": end_time,
        **name_params,
    }
    result = cache.execute(combined_sql, params)
    return {
        "cluster_id": cluster_id,
        # "unknown" when cluster_meta could not be read: the metric names below
        # are then Aurora's by fallback, so an empty metric timeline may mean
        # "wrong names" rather than "nothing happened".
        "engine_family": family if family_resolved else "unknown",
        # Which series were searched. Without this an empty timeline is
        # indistinguishable from a timeline of series that do not exist here.
        "metrics_included": list(metrics),
        "start_time": start_time,
        "end_time": end_time,
        "timeline": result.rows,
        "count": result.row_count,
    }
