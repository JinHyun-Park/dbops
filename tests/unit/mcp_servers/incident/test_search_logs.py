from unittest.mock import MagicMock, patch

from mcp_servers.incident.tools.search_logs import search_logs_impl


@patch("mcp_servers.incident.tools.search_logs.time")
@patch("mcp_servers.incident.tools.search_logs.client_for_cluster")
def test_search_logs_returns_results(mock_client_for, mock_time):
    mock_time.time.return_value = 1704067200.0
    mock_time.sleep = MagicMock()

    mock_logs_client = MagicMock()
    mock_client_for.return_value = mock_logs_client
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
    # Cross-account-aware: resolves the logs client for THIS cluster.
    mock_client_for.assert_called_once_with("prod-pg-1", "logs")


@patch("mcp_servers.incident.tools.search_logs.time")
@patch("mcp_servers.incident.tools.search_logs.client_for_cluster")
def test_search_logs_timeout(mock_client_for, mock_time):
    mock_time.time.return_value = 1704067200.0
    mock_time.sleep = MagicMock()

    mock_logs_client = MagicMock()
    mock_client_for.return_value = mock_logs_client
    mock_logs_client.start_query.return_value = {"queryId": "q-timeout"}
    mock_logs_client.get_query_results.return_value = {"status": "Running"}

    mock_cache = MagicMock()
    result = search_logs_impl(mock_cache, cluster_id="prod-pg-1")
    assert "error" in result
    assert "timed out" in result["error"]


# ===== log_group allowlist (E-0 review: agent-supplied parameter) =====


@patch("mcp_servers.incident.tools.search_logs.client_for_cluster")
def test_out_of_scope_log_group_refused_without_calling_aws(mock_client_for):
    """log_group is the one agent-controllable input that steers WHERE we read.
    A group outside the DB families must be refused BEFORE any AWS call, so a
    prompt-injected or hallucinated group cannot reach Lambda/application logs."""
    factory = MagicMock(side_effect=AssertionError("must not touch AWS"))
    mock_client_for.side_effect = factory
    for bad in (
        "/aws/lambda/dbops-api",
        "/aws/codebuild/secret-build",
        "my-app/prod",
        "/aws/rds",  # prefix-adjacent but not a cluster/instance group
    ):
        result = search_logs_impl(MagicMock(), cluster_id="prod-pg-1", log_group=bad)
        assert result["status"] == "log_group_not_allowed", bad
        assert result["count"] == 0 and result["results"] == []
        assert result["reason"]
    mock_client_for.assert_not_called()


@patch("mcp_servers.incident.tools.search_logs.time")
@patch("mcp_servers.incident.tools.search_logs.client_for_cluster")
def test_allowed_engine_log_groups_pass_through(mock_client_for, mock_time):
    """The three DB families DBOps reads must all be accepted, including the
    DocumentDB profiler group the set_docdb_profiler tool enables."""
    mock_time.time.return_value = 1704067200.0
    mock_time.sleep = MagicMock()
    client = MagicMock()
    mock_client_for.return_value = client
    client.start_query.return_value = {"queryId": "q-1"}
    client.get_query_results.return_value = {"status": "Complete", "results": []}

    for good in (
        "/aws/rds/cluster/prod-pg-1/error",
        "/aws/rds/instance/dbops-demo-mysql/slowquery",
        "/aws/docdb/docdb-1/profiler",
        "/aws/docdb/docdb-1/audit",
    ):
        result = search_logs_impl(MagicMock(), cluster_id="c", log_group=good)
        assert result.get("status") != "log_group_not_allowed", good
        assert result["log_group"] == good
