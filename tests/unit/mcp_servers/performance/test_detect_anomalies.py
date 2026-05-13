from unittest.mock import MagicMock

from mcp_servers.performance.tools.detect_anomalies import detect_anomalies_impl
from mcp_servers.shared.models import QueryResult


def test_detect_anomalies_returns_anomalous_metrics():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["metric_type", "current_avg", "baseline_avg", "baseline_stddev", "z_score"],
        rows=[
            {"metric_type": "aas", "current_avg": 8.5, "baseline_avg": 3.0, "baseline_stddev": 1.0, "z_score": 5.5},
            {"metric_type": "cpu", "current_avg": 45.0, "baseline_avg": 40.0, "baseline_stddev": 5.0, "z_score": 1.0},
        ],
        row_count=2,
    )
    result = detect_anomalies_impl(mock_cache, cluster_id="prod-pg-1", hours=4, threshold=2.0)
    assert result["cluster_id"] == "prod-pg-1"
    assert len(result["anomalies"]) > 0
    assert result["anomalies"][0]["metric_type"] == "aas"
