from unittest.mock import MagicMock
from mcp_servers.incident.tools.recent_events import get_recent_events_impl
from mcp_servers.shared.models import QueryResult


def test_recent_events_default():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["event_time", "event_type", "message"],
        rows=[
            {"event_time": "2024-01-01T00:00:00Z", "event_type": "failover", "message": "Failover completed"},
            {"event_time": "2024-01-01T01:00:00Z", "event_type": "restart", "message": "Instance restarted"},
        ],
        row_count=2,
    )
    result = get_recent_events_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["cluster_id"] == "prod-pg-1"
    assert result["count"] == 2
    assert len(result["events"]) == 2
    mock_cache.execute.assert_called_once()


def test_recent_events_with_event_type_filter():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    result = get_recent_events_impl(mock_cache, cluster_id="prod-pg-1", hours=12, event_type="failover")
    call_args = mock_cache.execute.call_args
    sql = call_args[0][0]
    params = call_args[0][1]
    assert "event_type = :event_type" in sql
    assert params["event_type"] == "failover"
    assert params["hours"] == 12
