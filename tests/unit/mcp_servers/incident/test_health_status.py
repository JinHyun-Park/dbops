from unittest.mock import MagicMock

from mcp_servers.incident.tools.health_status import get_health_status_impl
from mcp_servers.shared.models import QueryResult


def test_health_status_available_cluster():
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(
            columns=["cluster_id", "status", "engine", "instance_class"],
            rows=[{"cluster_id": "prod-pg-1", "status": "available", "engine": "aurora-postgresql", "instance_class": "db.r6g.xlarge"}],
            row_count=1,
        ),
        QueryResult(
            columns=["metric_type", "avg_val", "max_val"],
            rows=[
                {"metric_type": "cpu", "avg_val": 25.0, "max_val": 40.0},
                {"metric_type": "connections", "avg_val": 50.0, "max_val": 80.0},
            ],
            row_count=2,
        ),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["health"] == "healthy"
    assert result["cluster_id"] == "prod-pg-1"
    assert len(result["current_metrics"]) == 2
    assert mock_cache.execute.call_count == 2


def test_health_status_modifying_cluster():
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(
            columns=["cluster_id", "status"],
            rows=[{"cluster_id": "prod-pg-1", "status": "modifying"}],
            row_count=1,
        ),
        QueryResult(columns=[], rows=[], row_count=0),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["health"] == "warning"


def test_health_status_unknown_cluster():
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        QueryResult(columns=[], rows=[], row_count=0),
        QueryResult(columns=[], rows=[], row_count=0),
    ]
    result = get_health_status_impl(mock_cache, cluster_id="unknown-cluster")
    assert result["health"] == "critical"
