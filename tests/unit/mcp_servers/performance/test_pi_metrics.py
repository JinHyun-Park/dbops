from unittest.mock import MagicMock

from mcp_servers.performance.tools.pi_metrics import get_pi_metrics_impl
from mcp_servers.shared.models import QueryResult


def test_get_pi_metrics_filters_by_type():
    mock_cache = MagicMock()
    mock_cache._build_query.return_value = (
        "SELECT * FROM metric_snapshots WHERE cluster_id = :cluster_id AND metric_type = :metric_type ORDER BY ts ASC",
        {"cluster_id": "prod-pg-1"},
    )
    mock_cache.execute.return_value = QueryResult(
        columns=["ts", "value"], rows=[{"ts": "2026-05-01T00:00:00Z", "value": 3.2}], row_count=1
    )
    result = get_pi_metrics_impl(mock_cache, cluster_id="prod-pg-1", metric_type="aas")
    assert result["metric_type"] == "aas"
    assert result["count"] == 1
