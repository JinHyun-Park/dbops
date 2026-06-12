"""Tests for enable_dynamodb_pitr — 3-state flow, idempotency, TOCTOU drift,
and the force-to-disable rule (#7)."""

from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.enable_dynamodb_pitr import enable_dynamodb_pitr_impl


def _cache_with_name(name="ledger"):
    cache = MagicMock()
    cache.execute.return_value.rows = [{"resource_name": name}]
    return cache


def _ddb_client(*, status="DISABLED", describe_seq=None):
    client = MagicMock()

    def _desc(s):
        return {
            "ContinuousBackupsDescription": {
                "PointInTimeRecoveryDescription": {"PointInTimeRecoveryStatus": s}
            }
        }

    if describe_seq is not None:
        client.describe_continuous_backups.side_effect = [_desc(s) for s in describe_seq]
    else:
        client.describe_continuous_backups.return_value = _desc(status)
    return client


@patch("mcp_servers.operations.tools.enable_dynamodb_pitr.client_for_cluster")
def test_pitr_enable_requires_approval(mock_client_for):
    mock_client_for.return_value = _ddb_client(status="DISABLED")
    result = enable_dynamodb_pitr_impl(
        _cache_with_name(), cluster_id="ddb-abc", enabled=True
    )
    assert result["status"] == "approval_required"
    assert result["enabled"] is True


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.enable_dynamodb_pitr.client_for_cluster")
def test_pitr_enable_executes(mock_client_for):
    client = _ddb_client(status="DISABLED")
    mock_client_for.return_value = client
    result = enable_dynamodb_pitr_impl(
        _cache_with_name(), cluster_id="ddb-abc", enabled=True, approved=True
    )
    assert result["status"] == "modified"
    client.update_continuous_backups.assert_called_once()
    kwargs = client.update_continuous_backups.call_args.kwargs
    assert kwargs["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": True
    }


@patch("mcp_servers.operations.tools.enable_dynamodb_pitr.client_for_cluster")
def test_pitr_idempotent_skip(mock_client_for):
    client = _ddb_client(status="ENABLED")
    mock_client_for.return_value = client
    result = enable_dynamodb_pitr_impl(
        _cache_with_name(), cluster_id="ddb-abc", enabled=True
    )
    assert result["status"] == "skipped"
    client.update_continuous_backups.assert_not_called()


# ===== fix #7: force required to disable =====


@patch("mcp_servers.operations.tools.enable_dynamodb_pitr.client_for_cluster")
def test_pitr_disable_without_force_denied(mock_client_for):
    """fix #7: disabling PITR without force=true → error, no AWS read/write even
    started (refused before the describe round-trip)."""
    client = _ddb_client(status="ENABLED")
    mock_client_for.return_value = client
    result = enable_dynamodb_pitr_impl(
        _cache_with_name(), cluster_id="ddb-abc", enabled=False, force=False, approved=True
    )
    assert result["status"] == "error"
    assert "force" in result["reason"]
    client.update_continuous_backups.assert_not_called()


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.enable_dynamodb_pitr.client_for_cluster")
def test_pitr_disable_with_force_executes(mock_client_for):
    """fix #7: disabling with force=true (and a matching approval) → executes."""
    client = _ddb_client(status="ENABLED")
    mock_client_for.return_value = client
    result = enable_dynamodb_pitr_impl(
        _cache_with_name(), cluster_id="ddb-abc", enabled=False, force=True, approved=True
    )
    assert result["status"] == "modified"
    kwargs = client.update_continuous_backups.call_args.kwargs
    assert kwargs["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": False
    }


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.enable_dynamodb_pitr.client_for_cluster")
def test_pitr_toctou_drift_denied(mock_client_for):
    """fix #6: PITR already toggled between request and execute → denied."""
    client = _ddb_client(describe_seq=["DISABLED", "ENABLED"])
    mock_client_for.return_value = client
    result = enable_dynamodb_pitr_impl(
        _cache_with_name(), cluster_id="ddb-abc", enabled=True, approved=True
    )
    assert result["status"] == "approval_denied"
    client.update_continuous_backups.assert_not_called()


@patch("mcp_servers.operations.tools.enable_dynamodb_pitr.client_for_cluster")
def test_pitr_describe_failure_is_error(mock_client_for):
    client = MagicMock()
    client.describe_continuous_backups.side_effect = Exception("boom")
    mock_client_for.return_value = client
    result = enable_dynamodb_pitr_impl(
        _cache_with_name(), cluster_id="ddb-abc", enabled=True, approved=True
    )
    assert result["status"] == "error"
    client.update_continuous_backups.assert_not_called()
