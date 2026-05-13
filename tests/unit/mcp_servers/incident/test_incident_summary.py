from unittest.mock import MagicMock

from mcp_servers.incident.tools.incident_summary import get_incident_summary_impl
from mcp_servers.shared.models import QueryResult


def test_incident_summary_aggregates_events():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["event_type", "severity", "count", "first_seen", "last_seen"],
        rows=[
            {"event_type": "failover", "severity": "critical", "count": 3, "first_seen": "2024-01-01", "last_seen": "2024-01-15"},
            {"event_type": "restart", "severity": "warning", "count": 7, "first_seen": "2024-01-02", "last_seen": "2024-01-20"},
        ],
        row_count=2,
    )
    result = get_incident_summary_impl(mock_cache, cluster_id="prod-pg-1", days=30)
    assert result["cluster_id"] == "prod-pg-1"
    assert result["period_days"] == 30
    assert result["total_events"] == 10
    assert len(result["summary"]) == 2


def test_incident_summary_empty():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    result = get_incident_summary_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["total_events"] == 0
    assert result["summary"] == []
