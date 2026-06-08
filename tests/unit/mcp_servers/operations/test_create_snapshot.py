"""Tests for the agent-side create_snapshot MCP tool (approval-gated)."""

from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.create_snapshot import (
    _make_snapshot_id,
    create_snapshot_impl,
)


def test_requires_approval_without_approved_flag():
    cache = MagicMock()
    out = create_snapshot_impl(cache, cluster_id="prod-pg-1")
    assert out["status"] == "approval_required"
    assert out["cluster_id"] == "prod-pg-1"


@patch("mcp_servers.operations.tools.create_snapshot.verify_approval")
def test_approval_denied_when_guard_rejects(mock_guard):
    mock_guard.return_value = {"ok": False, "reason": "stale"}
    cache = MagicMock()
    out = create_snapshot_impl(
        cache, cluster_id="prod-pg-1", approved=True, approval_id="aid-1"
    )
    assert out["status"] == "approval_denied"
    assert "stale" in out["reason"]
    mock_guard.assert_called_once_with(
        "aid-1", "prod-pg-1", "create_snapshot", payload={"snapshot_id": ""}
    )


@patch("mcp_servers.operations.tools.create_snapshot.boto3")
@patch("mcp_servers.operations.tools.create_snapshot.verify_approval")
def test_creates_snapshot_when_approved(mock_guard, mock_boto3):
    mock_guard.return_value = {"ok": True}
    rds = MagicMock()
    rds.create_db_cluster_snapshot.return_value = {
        "DBClusterSnapshot": {"Status": "creating"}
    }
    mock_boto3.client.return_value = rds
    cache = MagicMock()

    out = create_snapshot_impl(
        cache,
        cluster_id="prod-pg-1",
        snapshot_id="my-manual-snap",
        approved=True,
        approval_id="aid-1",
    )
    assert out["status"] == "creating"
    assert out["snapshot_id"] == "my-manual-snap"
    call = rds.create_db_cluster_snapshot.call_args.kwargs
    assert call["DBClusterSnapshotIdentifier"] == "my-manual-snap"
    assert call["DBClusterIdentifier"] == "prod-pg-1"


@patch("mcp_servers.operations.tools.create_snapshot.boto3")
@patch("mcp_servers.operations.tools.create_snapshot.verify_approval")
def test_auto_generates_id_when_omitted(mock_guard, mock_boto3):
    mock_guard.return_value = {"ok": True}
    rds = MagicMock()
    rds.create_db_cluster_snapshot.return_value = {"DBClusterSnapshot": {"Status": "creating"}}
    mock_boto3.client.return_value = rds
    cache = MagicMock()

    out = create_snapshot_impl(
        cache, cluster_id="prod-pg-1", approved=True, approval_id="aid-1"
    )
    assert out["snapshot_id"].startswith("manual-")
    assert out["status"] == "creating"


@patch("mcp_servers.operations.tools.create_snapshot.verify_approval")
def test_invalid_snapshot_id_rejected(mock_guard):
    mock_guard.return_value = {"ok": True}
    cache = MagicMock()
    out = create_snapshot_impl(
        cache,
        cluster_id="prod-pg-1",
        snapshot_id="-bad--id-",  # leading hyphen + double hyphen
        approved=True,
        approval_id="aid-1",
    )
    assert out["status"] == "invalid_snapshot_id"


@patch("mcp_servers.operations.tools.create_snapshot.boto3")
@patch("mcp_servers.operations.tools.create_snapshot.verify_approval")
def test_create_failure_surfaced(mock_guard, mock_boto3):
    mock_guard.return_value = {"ok": True}
    rds = MagicMock()
    rds.create_db_cluster_snapshot.side_effect = RuntimeError("boom")
    mock_boto3.client.return_value = rds
    cache = MagicMock()
    out = create_snapshot_impl(
        cache, cluster_id="prod-pg-1", approved=True, approval_id="aid-1"
    )
    assert out["status"] == "create_failed"
    assert "boom" in out["error"]


def test_make_snapshot_id_is_valid():
    import re

    sid = _make_snapshot_id("dbops-dev-sample-samplepg789869c8-caf4ladtqz0i")
    assert re.match(r"^[a-zA-Z][a-zA-Z0-9]*(-[a-zA-Z0-9]+)*$", sid)
    assert len(sid) <= 63
    assert "--" not in sid
    assert not sid.endswith("-")
