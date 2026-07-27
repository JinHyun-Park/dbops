from unittest.mock import MagicMock

from mcp_servers.incident.tools.correlate_signals import correlate_signals_impl
from mcp_servers.shared.models import QueryResult


def _cache(engine):
    """cache.execute answers the cluster_meta engine lookup, then the timeline."""
    cache = MagicMock()
    timeline = QueryResult(
        columns=["event_time", "signal_type", "detail", "value"],
        rows=[
            {"event_time": "2024-01-01T00:00:00Z", "signal_type": "metric", "detail": "cpu", "value": "85.0"},
            {"event_time": "2024-01-01T00:01:00Z", "signal_type": "event", "detail": "failover", "value": "Failover started"},
            {"event_time": "2024-01-01T00:02:00Z", "signal_type": "metric", "detail": "connections", "value": "0"},
        ],
        row_count=3,
    )

    def _side(sql, params=None):
        if "cluster_meta" in sql:
            rows = [{"engine": engine}] if engine else []
            return QueryResult(columns=["engine"], rows=rows, row_count=len(rows))
        return timeline

    cache.execute.side_effect = _side
    return cache


def _timeline_metrics(cache):
    for call in cache.execute.call_args_list:
        if "UNION ALL" in call[0][0]:
            params = call[0][1]
            return sorted(v for k, v in params.items() if k.startswith("m"))
    raise AssertionError("timeline query was never issued")


def _run(cache):
    return correlate_signals_impl(
        cache,
        cluster_id="prod-pg-1",
        start_time="2024-01-01T00:00:00Z",
        end_time="2024-01-01T01:00:00Z",
    )


def test_correlate_signals_returns_timeline():
    cache = _cache("aurora-postgresql")
    result = _run(cache)
    assert result["cluster_id"] == "prod-pg-1"
    assert result["count"] == 3
    assert len(result["timeline"]) == 3
    sql = cache.execute.call_args[0][0]
    assert "UNION ALL" in sql


def test_relational_metric_names_unchanged():
    cache = _cache("aurora-postgresql")
    result = _run(cache)
    assert result["engine_family"] == "relational"
    assert _timeline_metrics(cache) == ["aas", "cpu", "db_connections"]
    assert sorted(result["metrics_included"]) == ["aas", "cpu", "db_connections"]


def test_documentdb_timeline_uses_its_own_metric_names():
    cache = _cache("docdb")
    result = _run(cache)
    assert result["engine_family"] == "documentdb"
    searched = _timeline_metrics(cache)
    assert "cpu_utilization" in searched  # 'cpu' does not exist for DocumentDB
    assert "cursors_timed_out" in searched
    assert "cpu" not in searched


def test_elasticache_timeline_includes_evictions_and_lag():
    """Ranked elsewhere, but a correlation timeline without them is useless."""
    cache = _cache("valkey")
    result = _run(cache)
    assert result["engine_family"] == "elasticache"
    searched = _timeline_metrics(cache)
    assert {"cache_cpu", "engine_cpu", "evictions", "replication_lag"} <= set(searched)


def test_dynamodb_timeline_uses_throttle_series():
    cache = _cache("dynamodb")
    result = _run(cache)
    assert result["engine_family"] == "dynamodb"
    searched = _timeline_metrics(cache)
    assert "read_throttle_events" in searched and "consumed_rcu" in searched


def test_unregistered_cluster_reports_unknown_family():
    cache = _cache(None)
    result = _run(cache)
    assert result["engine_family"] == "unknown"
    # still returns a usable timeline, but names the series it searched so an
    # empty metric timeline cannot be misread as "nothing happened"
    assert result["metrics_included"] == ["aas", "cpu", "db_connections"]
