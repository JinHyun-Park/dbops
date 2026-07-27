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


# ===== Insights is not SQL (live regression 2026-07-24) =====


@patch("mcp_servers.incident.tools.search_logs.time")
@patch("mcp_servers.incident.tools.search_logs.client_for_cluster")
def test_query_is_sent_verbatim_without_the_sql_audit_comment(mock_client_for, mock_time):
    """The `/* source=dbops-agent */` marker is the audit convention for SQL sent
    to a TARGET DATABASE. CloudWatch Logs Insights is not SQL: it rejects the
    comment with MalformedQueryException BEFORE it resolves the log group, so
    prefixing it made every search_logs call fail on every cluster and engine.
    Verified live against a real log group: with the comment the API returns
    MalformedQueryException, without it the query parses."""
    mock_time.time.return_value = 1704067200.0
    mock_time.sleep = MagicMock()
    client = MagicMock()
    mock_client_for.return_value = client
    client.start_query.return_value = {"queryId": "q-1"}
    client.get_query_results.return_value = {"status": "Complete", "results": []}

    q = "fields @timestamp, @message | filter @message like /ERROR/ | limit 5"
    search_logs_impl(MagicMock(), cluster_id="prod-pg-1", query=q)

    sent = client.start_query.call_args.kwargs["queryString"]
    assert sent == q
    assert "/*" not in sent and "source=dbops-agent" not in sent


def _boto_error_client(exc_name):
    """MagicMock whose start_query raises the named botocore modeled exception,
    with client.exceptions wired the way botocore exposes it."""
    class _Err(Exception):
        pass

    client = MagicMock()
    setattr(client.exceptions, exc_name, _Err)
    # the other modeled exception must still be a real class for the except clause
    other = "MalformedQueryException" if exc_name == "ResourceNotFoundException" else "ResourceNotFoundException"

    class _Other(Exception):
        pass

    setattr(client.exceptions, other, _Other)
    client.start_query.side_effect = _Err("boom: arn:aws:logs:secret-ish")
    return client


@patch("mcp_servers.incident.tools.search_logs.client_for_cluster")
def test_missing_log_group_is_an_operator_message_not_an_internal_error(mock_client_for):
    """A missing group means log exports are off for that cluster. Surfacing the
    generic handler tool_error hid that and told the DBA nothing actionable."""
    client = _boto_error_client("ResourceNotFoundException")
    mock_client_for.return_value = client
    result = search_logs_impl(
        MagicMock(), cluster_id="prod-pg-1",
        log_group="/aws/rds/cluster/prod-pg-1/error",
    )
    assert result["status"] == "log_group_not_found"
    assert "내보내기" in result["reason"]
    assert result["count"] == 0 and result["results"] == []
    # no raw exception text in the response
    blob = " ".join(str(v) for v in result.values())
    assert "boom" not in blob and "secret-ish" not in blob


@patch("mcp_servers.incident.tools.search_logs.client_for_cluster")
def test_malformed_query_explains_the_insights_syntax(mock_client_for):
    client = _boto_error_client("MalformedQueryException")
    mock_client_for.return_value = client
    result = search_logs_impl(
        MagicMock(), cluster_id="prod-pg-1", query="SELECT * FROM logs",
    )
    assert result["status"] == "malformed_query"
    assert "SQL" in result["reason"]
    blob = " ".join(str(v) for v in result.values())
    assert "boom" not in blob
