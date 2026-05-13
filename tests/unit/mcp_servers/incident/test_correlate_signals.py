from unittest.mock import MagicMock

from mcp_servers.incident.tools.correlate_signals import correlate_signals_impl
from mcp_servers.shared.models import QueryResult


def test_correlate_signals_returns_timeline():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["event_time", "signal_type", "detail", "value"],
        rows=[
            {"event_time": "2024-01-01T00:00:00Z", "signal_type": "metric", "detail": "cpu", "value": "85.0"},
            {"event_time": "2024-01-01T00:01:00Z", "signal_type": "event", "detail": "failover", "value": "Failover started"},
            {"event_time": "2024-01-01T00:02:00Z", "signal_type": "metric", "detail": "connections", "value": "0"},
        ],
        row_count=3,
    )
    result = correlate_signals_impl(
        mock_cache,
        cluster_id="prod-pg-1",
        start_time="2024-01-01T00:00:00Z",
        end_time="2024-01-01T01:00:00Z",
    )
    assert result["cluster_id"] == "prod-pg-1"
    assert result["count"] == 3
    assert len(result["timeline"]) == 3
    mock_cache.execute.assert_called_once()
    sql = mock_cache.execute.call_args[0][0]
    assert "UNION ALL" in sql
