from unittest.mock import MagicMock, patch
from mcp_servers.incident.tools.search_logs import search_logs_impl


@patch("mcp_servers.incident.tools.search_logs.time")
@patch("mcp_servers.incident.tools.search_logs.boto3")
def test_search_logs_returns_results(mock_boto3, mock_time):
    mock_time.time.return_value = 1704067200.0
    mock_time.sleep = MagicMock()

    mock_logs_client = MagicMock()
    mock_boto3.client.return_value = mock_logs_client
    mock_logs_client.start_query.return_value = {"queryId": "q-123"}
    mock_logs_client.get_query_results.return_value = {
        "status": "Complete",
        "results": [
            [{"field": "@timestamp", "value": "2024-01-01T00:00:00"}, {"field": "@message", "value": "ERROR: deadlock detected"}],
            [{"field": "@timestamp", "value": "2024-01-01T00:01:00"}, {"field": "@message", "value": "ERROR: connection reset"}],
        ],
    }

    mock_cache = MagicMock()
    result = search_logs_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["cluster_id"] == "prod-pg-1"
    assert result["count"] == 2
    assert result["log_group"] == "/aws/rds/cluster/prod-pg-1/error"
    assert result["results"][0]["@message"] == "ERROR: deadlock detected"


@patch("mcp_servers.incident.tools.search_logs.time")
@patch("mcp_servers.incident.tools.search_logs.boto3")
def test_search_logs_timeout(mock_boto3, mock_time):
    mock_time.time.return_value = 1704067200.0
    mock_time.sleep = MagicMock()

    mock_logs_client = MagicMock()
    mock_boto3.client.return_value = mock_logs_client
    mock_logs_client.start_query.return_value = {"queryId": "q-timeout"}
    mock_logs_client.get_query_results.return_value = {"status": "Running"}

    mock_cache = MagicMock()
    result = search_logs_impl(mock_cache, cluster_id="prod-pg-1")
    assert "error" in result
    assert "timed out" in result["error"]
