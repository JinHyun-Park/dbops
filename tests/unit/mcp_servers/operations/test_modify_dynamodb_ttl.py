"""Tests for modify_dynamodb_ttl — 3-state flow, idempotency skip, TOCTOU drift,
and the never-raise error path."""

from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.modify_dynamodb_ttl import modify_dynamodb_ttl_impl


def _cache_with_name(name="sessions"):
    cache = MagicMock()
    cache.execute.return_value.rows = [{"resource_name": name}]
    return cache


def _ddb_client(*, status="DISABLED", attribute="", describe_seq=None):
    client = MagicMock()

    def _desc(s, a):
        return {"TimeToLiveDescription": {"TimeToLiveStatus": s, "AttributeName": a}}

    if describe_seq is not None:
        client.describe_time_to_live.side_effect = [_desc(s, a) for (s, a) in describe_seq]
    else:
        client.describe_time_to_live.return_value = _desc(status, attribute)
    return client


@patch("mcp_servers.operations.tools.modify_dynamodb_ttl.client_for_cluster")
def test_ttl_requires_approval(mock_client_for):
    mock_client_for.return_value = _ddb_client(status="DISABLED")
    result = modify_dynamodb_ttl_impl(
        _cache_with_name(), cluster_id="ddb-abc", attribute="expires_at", enabled=True
    )
    assert result["status"] == "approval_required"
    assert result["attribute"] == "expires_at"
    assert result["enabled"] is True


@patch("mcp_servers.operations.tools.modify_dynamodb_ttl.client_for_cluster")
def test_ttl_missing_attribute_error(mock_client_for):
    mock_client_for.return_value = _ddb_client(status="DISABLED")
    result = modify_dynamodb_ttl_impl(
        _cache_with_name(), cluster_id="ddb-abc", attribute="", enabled=True
    )
    assert result["status"] == "error"


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_dynamodb_ttl.client_for_cluster")
def test_ttl_approved_executes(mock_client_for):
    client = _ddb_client(status="DISABLED")
    mock_client_for.return_value = client
    result = modify_dynamodb_ttl_impl(
        _cache_with_name(),
        cluster_id="ddb-abc",
        attribute="expires_at",
        enabled=True,
        approved=True,
    )
    assert result["status"] == "modified"
    client.update_time_to_live.assert_called_once()
    kwargs = client.update_time_to_live.call_args.kwargs
    assert kwargs["TimeToLiveSpecification"] == {
        "Enabled": True,
        "AttributeName": "expires_at",
    }


@patch("mcp_servers.operations.tools.modify_dynamodb_ttl.client_for_cluster")
def test_ttl_idempotent_skip(mock_client_for):
    """Already enabled on the same attribute → skipped, no write."""
    client = _ddb_client(status="ENABLED", attribute="expires_at")
    mock_client_for.return_value = client
    result = modify_dynamodb_ttl_impl(
        _cache_with_name(), cluster_id="ddb-abc", attribute="expires_at", enabled=True
    )
    assert result["status"] == "skipped"
    client.update_time_to_live.assert_not_called()


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_dynamodb_ttl.client_for_cluster")
def test_ttl_toctou_drift_denied(mock_client_for):
    """fix #6: state drifted between request and execute → denied, no write."""
    client = _ddb_client(
        describe_seq=[
            ("DISABLED", ""),
            ("ENABLED", "expires_at"),  # someone enabled it meanwhile
        ]
    )
    mock_client_for.return_value = client
    result = modify_dynamodb_ttl_impl(
        _cache_with_name(),
        cluster_id="ddb-abc",
        attribute="expires_at",
        enabled=True,
        approved=True,
    )
    assert result["status"] == "approval_denied"
    client.update_time_to_live.assert_not_called()


@patch("mcp_servers.operations.tools.modify_dynamodb_ttl.client_for_cluster")
def test_ttl_describe_failure_is_error(mock_client_for):
    client = MagicMock()
    client.describe_time_to_live.side_effect = Exception("boom")
    mock_client_for.return_value = client
    result = modify_dynamodb_ttl_impl(
        _cache_with_name(), cluster_id="ddb-abc", attribute="expires_at", approved=True
    )
    assert result["status"] == "error"
    client.update_time_to_live.assert_not_called()
