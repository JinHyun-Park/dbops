"""Tests for the server-side approval guard.

These tests cover every reason the guard can reject — missing id, wrong
cluster, wrong action_type, stale resolved_at, status not approved,
already-consumed, missing resolved_at, missing env var. The atomic
consume path is also tested via the ConditionalCheckFailedException
branch.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from mcp_servers.shared.approval_guard import REPLAY_WINDOW_SECONDS, verify_approval


def _iso_now(delta_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


def _fresh_row(approval_id="aid-1", cluster_id="prod-pg-1", action_type="execute_sql", status="approved"):
    return {
        "approval_id": approval_id,
        "created_at": "1716903000000",
        "approval_status": status,
        "cluster_id": cluster_id,
        "action_type": action_type,
        "resolved_at": _iso_now(),
    }


def _scan_returning(item):
    """Build a mock DDB table that returns `item` for scan() and accepts
    update_item() without raising."""
    table = MagicMock()
    table.scan.return_value = {"Items": [item]} if item else {"Items": []}
    return table


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_happy_path(mock_boto3):
    table = _scan_returning(_fresh_row())
    mock_boto3.resource.return_value.Table.return_value = table

    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")

    assert result == {"ok": True}
    # Atomic consume must be called with the conditional check.
    table.update_item.assert_called_once()
    call = table.update_item.call_args.kwargs
    assert "ConditionExpression" in call
    assert ":approved" in call["ExpressionAttributeValues"]


def test_verify_approval_missing_id_rejects():
    result = verify_approval("", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "approval_id missing" in result["reason"]


@patch.dict("os.environ", {}, clear=True)
def test_verify_approval_missing_table_env_fails_closed():
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "APPROVALS_TABLE" in result["reason"]


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"}, clear=True)
def test_verify_approval_bypass_env_for_local_dev():
    """The bypass env var lets local dev skip approval but must NOT be
    settable from the agent. It's gated by the deployment, not the call."""
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is True
    assert result.get("bypass") is True


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_unknown_id(mock_boto3):
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(None)
    result = verify_approval("aid-missing", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "not found" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_status_pending_rejected(mock_boto3):
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(
        _fresh_row(status="pending")
    )
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "pending" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_status_rejected_path(mock_boto3):
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(
        _fresh_row(status="rejected")
    )
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "rejected" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_already_consumed(mock_boto3):
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(
        _fresh_row(status="consumed")
    )
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "consumed" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_wrong_cluster(mock_boto3):
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(
        _fresh_row(cluster_id="prod-pg-1")
    )
    result = verify_approval("aid-1", "DIFFERENT-cluster", "execute_sql")
    assert result["ok"] is False
    assert "prod-pg-1" in result["reason"]
    assert "DIFFERENT-cluster" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_wrong_action_type(mock_boto3):
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(
        _fresh_row(action_type="modify_parameter")
    )
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "modify_parameter" in result["reason"]
    assert "execute_sql" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_action_type_other_is_permissive(mock_boto3):
    """If the approval row was registered with action_type='other' the
    tool's specific action_type still has to match — but if both sides
    say 'other', it passes."""
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(
        _fresh_row(action_type="other")
    )
    result = verify_approval("aid-1", "prod-pg-1", "other")
    assert result["ok"] is True


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_stale_rejected(mock_boto3):
    row = _fresh_row()
    row["resolved_at"] = _iso_now(delta_seconds=-(REPLAY_WINDOW_SECONDS + 60))
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(row)

    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "old" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_no_resolved_at(mock_boto3):
    row = _fresh_row()
    row["resolved_at"] = ""
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(row)
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "resolved_at" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_garbage_resolved_at(mock_boto3):
    row = _fresh_row()
    row["resolved_at"] = "not-a-date"
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(row)
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_concurrent_consume_loses(mock_boto3):
    """The atomic consume can race — if another process consumed first,
    we get ConditionalCheckFailedException and must reject."""
    table = _scan_returning(_fresh_row())
    err = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "race"}},
        "UpdateItem",
    )
    table.update_item.side_effect = err
    mock_boto3.resource.return_value.Table.return_value = table

    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "concurrent" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_zulu_timestamp_parses(mock_boto3):
    """resolved_at often ends with Z when the writer uses utcnow().isoformat()
    + 'Z'. The guard must parse this correctly."""
    row = _fresh_row()
    row["resolved_at"] = "2026-05-28T10:00:00Z"
    # Make resolved_at "now" by patching time.time inside the guard.
    with patch("mcp_servers.shared.approval_guard.time.time") as mock_time:
        # Pretend it's only 60s after the resolved_at timestamp.
        from datetime import datetime as _dt
        mock_time.return_value = _dt.fromisoformat("2026-05-28T10:01:00+00:00").timestamp()
        mock_boto3.resource.return_value.Table.return_value = _scan_returning(row)

        result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
        assert result["ok"] is True


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_ddb_scan_error_rejected(mock_boto3):
    """If DDB itself errors during scan, fail-closed (don't execute write)."""
    table = MagicMock()
    table.scan.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError"}}, "Scan",
    )
    mock_boto3.resource.return_value.Table.return_value = table

    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "not found" in result["reason"]
