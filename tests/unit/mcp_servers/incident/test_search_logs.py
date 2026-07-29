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
    """The three DB families DBOps reads must all be accepted for the cluster
    being investigated, including the DocumentDB profiler group the
    set_docdb_profiler tool enables."""
    mock_time.time.return_value = 1704067200.0
    mock_time.sleep = MagicMock()
    client = MagicMock()
    mock_client_for.return_value = client
    client.start_query.return_value = {"queryId": "q-1"}
    client.get_query_results.return_value = {"status": "Complete", "results": []}

    for cid, good in (
        ("prod-pg-1", "/aws/rds/cluster/prod-pg-1/error"),
        ("dbops-demo-mysql", "/aws/rds/instance/dbops-demo-mysql/slowquery"),
        ("docdb-1", "/aws/docdb/docdb-1/profiler"),
        ("docdb-1", "/aws/docdb/docdb-1/audit"),
    ):
        result = search_logs_impl(MagicMock(), cluster_id=cid, log_group=good)
        assert result.get("status") != "log_group_not_allowed", good
        assert result["log_group"] == good


# ===== the allowlist must bind to the CLUSTER, not just the family =====


@patch("mcp_servers.incident.tools.search_logs.client_for_cluster")
def test_another_clusters_log_group_is_refused_even_when_cluster_id_is_visible(mock_client_for):
    """TENANCY. agent/tool_gate.py's ClusterVisibilityGate inspects only
    args["cluster_id"], and the hub IAM grant covers the whole
    /aws/rds/cluster/* prefix, so a family-only allowlist let a caller scoped to
    team A pass a cluster_id it CAN see plus team B's log group and read another
    team's database logs. Nothing above this tool is cluster-aware for log_group,
    so the binding must hold here."""
    mock_client_for.side_effect = AssertionError("must not touch AWS")
    for bad in (
        "/aws/rds/cluster/team-b-pg/postgresql",
        "/aws/rds/cluster/team-b-pg/slowquery",
        "/aws/rds/instance/team-b-mysql/error",
        "/aws/docdb/team-b-docdb/profiler",
        # prefix-confusion attempts against the visible cluster's own name
        "/aws/rds/cluster/team-a-pg-evil/postgresql",
        "/aws/rds/cluster/team-a-pg",
    ):
        result = search_logs_impl(MagicMock(), cluster_id="team-a-pg", log_group=bad)
        assert result["status"] == "log_group_not_allowed", bad
        assert result["count"] == 0 and result["results"] == []
        assert result["reason"]
        # the refusal must not name another team's group as an acceptable option
        assert "team-b" not in result["reason"]
    mock_client_for.assert_not_called()


@patch("mcp_servers.incident.tools.search_logs.time")
@patch("mcp_servers.incident.tools.search_logs.client_for_cluster")
def test_the_clusters_own_log_groups_still_pass(mock_client_for, mock_time):
    """The other half of the same fix: every group belonging to the requested
    cluster must still be readable, or the tool is useless for MySQL slowquery
    and the DocumentDB profiler."""
    mock_time.time.return_value = 1704067200.0
    mock_time.sleep = MagicMock()
    client = MagicMock()
    mock_client_for.return_value = client
    client.start_query.return_value = {"queryId": "q-1"}
    client.get_query_results.return_value = {"status": "Complete", "results": []}

    for good in (
        "/aws/rds/cluster/team-a-pg/postgresql",
        "/aws/rds/cluster/team-a-pg/slowquery",
        "/aws/rds/cluster/team-a-pg/general",
        "/aws/rds/cluster/team-a-pg/audit",
        "/aws/rds/instance/team-a-pg/slowquery",
        "/aws/docdb/team-a-pg/profiler",
    ):
        result = search_logs_impl(MagicMock(), cluster_id="team-a-pg", log_group=good)
        assert result.get("status") != "log_group_not_allowed", good
        assert result["log_group"] == good


@patch("mcp_servers.incident.tools.search_logs.client_for_cluster")
def test_unusable_cluster_id_refuses_every_group(mock_client_for):
    """Fail closed: with no usable cluster_id there is nothing to bind the group
    to, so it must refuse rather than fall back to the family prefix."""
    mock_client_for.side_effect = AssertionError("must not touch AWS")
    for cid in ("", "   ", None, "../other-team"):
        result = search_logs_impl(
            MagicMock(), cluster_id=cid, log_group="/aws/rds/cluster/prod-pg-1/error"
        )
        assert result["status"] == "log_group_not_allowed", cid
        assert result["reason"]
    mock_client_for.assert_not_called()


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


@patch("mcp_servers.incident.tools.search_logs.client_for_cluster")
def test_malformed_query_is_not_echoed_into_the_logs(mock_client_for, caplog):
    """An incident-investigation query carries the value being hunted (an email,
    an account id, a token seen in a log line). Logging it would copy that
    content into a second, longer-lived log group, and it is not needed: the
    caller wrote the query and the response says what is wrong with it."""
    import logging

    client = _boto_error_client("MalformedQueryException")
    mock_client_for.return_value = client
    secret_term = "user@example.com"
    with caplog.at_level(logging.WARNING):
        result = search_logs_impl(
            MagicMock(), cluster_id="prod-pg-1",
            query=f"fields @timestamp | filter @message like /{secret_term}/ | limitt 5",
        )
    assert result["status"] == "malformed_query"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert secret_term not in logged
    assert "filter @message" not in logged
    # but the event itself must still be observable, with enough shape to tell a
    # truncated query from a wrong-dialect one
    assert "malformed" in logged.lower()
    assert "prod-pg-1" in logged


# ===== family-aware default log group (E-3) =====
# A standalone RDS DB instance publishes to /aws/rds/instance/<id>/..., so the
# Aurora /aws/rds/cluster/ default guaranteed a "log group not found" on every
# default call for that family. MEASURED on the live fixture: both
# /aws/rds/instance/dbops-demo-mysql/error and .../slowquery EXIST, and no
# /aws/rds/cluster/dbops-demo-mysql/* group does.

def _logs_double(mock_client_for, mock_time):
    mock_time.time.return_value = 1704067200.0
    mock_time.sleep = MagicMock()
    c = MagicMock()
    mock_client_for.return_value = c
    c.start_query.return_value = {"queryId": "q-1"}
    c.get_query_results.return_value = {"status": "Complete", "results": []}
    return c


def _cache_with_engine(engine):
    cache = MagicMock()
    cache.engine_of.return_value = engine
    return cache


@patch("mcp_servers.incident.tools.search_logs.time")
@patch("mcp_servers.incident.tools.search_logs.client_for_cluster")
def test_default_log_group_uses_the_instance_path_for_rds_instance(mock_client_for, mock_time):
    logs = _logs_double(mock_client_for, mock_time)
    for engine in ("mysql", "sqlserver-ex"):
        result = search_logs_impl(_cache_with_engine(engine), cluster_id="dbops-demo-mysql")
        assert result["log_group"] == "/aws/rds/instance/dbops-demo-mysql/error"
        assert result.get("status") != "log_group_not_allowed"
    assert logs.start_query.call_args.kwargs["logGroupName"] == \
        "/aws/rds/instance/dbops-demo-mysql/error"


@patch("mcp_servers.incident.tools.search_logs.time")
@patch("mcp_servers.incident.tools.search_logs.client_for_cluster")
def test_default_log_group_stays_on_the_cluster_path_for_aurora(mock_client_for, mock_time):
    """RELATIONAL REGRESSION PIN: Aurora keeps the cluster path."""
    _logs_double(mock_client_for, mock_time)
    for engine in ("aurora-postgresql", "aurora-mysql"):
        result = search_logs_impl(_cache_with_engine(engine), cluster_id="prod-pg-1")
        assert result["log_group"] == "/aws/rds/cluster/prod-pg-1/error"


@patch("mcp_servers.incident.tools.search_logs.time")
@patch("mcp_servers.incident.tools.search_logs.client_for_cluster")
def test_unresolvable_engine_keeps_the_historical_aurora_default(mock_client_for, mock_time):
    """engine_of() returns "" on ANY lookup failure. Guessing the instance path
    from a failed lookup would break Aurora on a transient cache error, so ""
    must keep the Aurora default (engine_family("") is relational)."""
    _logs_double(mock_client_for, mock_time)
    result = search_logs_impl(_cache_with_engine(""), cluster_id="prod-pg-1")
    assert result["log_group"] == "/aws/rds/cluster/prod-pg-1/error"


@patch("mcp_servers.incident.tools.search_logs.time")
@patch("mcp_servers.incident.tools.search_logs.client_for_cluster")
def test_explicit_log_group_still_wins_and_is_still_bound_to_the_cluster(mock_client_for, mock_time):
    _logs_double(mock_client_for, mock_time)
    cache = _cache_with_engine("mysql")
    ok = search_logs_impl(cache, cluster_id="dbops-demo-mysql",
                          log_group="/aws/rds/instance/dbops-demo-mysql/slowquery")
    assert ok["log_group"] == "/aws/rds/instance/dbops-demo-mysql/slowquery"
    assert ok.get("status") != "log_group_not_allowed"
    # Another cluster's instance log group is still refused.
    bad = search_logs_impl(cache, cluster_id="dbops-demo-mysql",
                           log_group="/aws/rds/instance/someone-elses-db/error")
    assert bad["status"] == "log_group_not_allowed"
