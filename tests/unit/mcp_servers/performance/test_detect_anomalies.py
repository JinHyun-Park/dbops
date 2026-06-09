from unittest.mock import MagicMock

from mcp_servers.performance.tools.detect_anomalies import detect_anomalies_impl
from mcp_servers.shared.models import QueryResult


def _rows(rows):
    cols = ["metric_type", "recent_max", "recent_avg", "baseline_mean",
            "baseline_stddev", "z_score", "mode", "sample_count"]
    return QueryResult(columns=cols, rows=rows, row_count=len(rows))


def test_detect_anomalies_filters_by_threshold():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = _rows([
        {"metric_type": "aas", "recent_max": 8.5, "recent_avg": 6.0, "baseline_mean": 3.0,
         "baseline_stddev": 1.0, "z_score": 5.5, "mode": "seasonal", "sample_count": 120},
        {"metric_type": "cpu", "recent_max": 45.0, "recent_avg": 42.0, "baseline_mean": 40.0,
         "baseline_stddev": 5.0, "z_score": 1.0, "mode": "seasonal", "sample_count": 120},
    ])
    result = detect_anomalies_impl(mock_cache, cluster_id="prod-pg-1", hours=4, threshold=2.0)
    assert result["cluster_id"] == "prod-pg-1"
    assert len(result["anomalies"]) == 1  # only aas (z=5.5) clears z>=2
    assert result["anomalies"][0]["metric_type"] == "aas"
    assert result["total_checked"] == 2


def test_detect_anomalies_uses_seasonal_baseline_query():
    """The query must read metric_baselines (seasonal) not just a flat stddev."""
    mock_cache = MagicMock()
    mock_cache.execute.return_value = _rows([])
    detect_anomalies_impl(mock_cache, cluster_id="prod-pg-1")
    sql = mock_cache.execute.call_args.args[0]
    assert "metric_baselines" in sql
    assert "hour_of_week" in sql
    assert "iqr" in sql.lower()


def test_detect_anomalies_reports_baseline_mode():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = _rows([
        {"metric_type": "aas", "recent_max": 8.5, "recent_avg": 6.0, "baseline_mean": 3.0,
         "baseline_stddev": 1.0, "z_score": 5.5, "mode": "seasonal", "sample_count": 120},
    ])
    result = detect_anomalies_impl(mock_cache, cluster_id="prod-pg-1", threshold=2.0)
    assert result["baseline_mode"] == "seasonal"


def test_detect_anomalies_flat_fallback_mode():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = _rows([
        {"metric_type": "connections", "recent_max": 900, "recent_avg": 850, "baseline_mean": 400,
         "baseline_stddev": 100, "z_score": 5.0, "mode": "flat", "sample_count": None},
    ])
    result = detect_anomalies_impl(mock_cache, cluster_id="prod-pg-1", threshold=2.0)
    assert result["baseline_mode"] == "flat"
    assert len(result["anomalies"]) == 1


def test_detect_anomalies_empty_is_none_mode():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = _rows([])
    result = detect_anomalies_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["baseline_mode"] == "none"
    assert result["anomalies"] == []
