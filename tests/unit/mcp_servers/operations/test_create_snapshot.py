"""Tests for the agent-side create_snapshot MCP tool (approval-gated)."""

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
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


@patch("mcp_servers.operations.tools.create_snapshot.rds_client_for_cluster")
@patch("mcp_servers.operations.tools.create_snapshot.verify_approval")
def test_creates_snapshot_when_approved(mock_guard, mock_rds_for):
    mock_guard.return_value = {"ok": True}
    rds = MagicMock()
    rds.create_db_cluster_snapshot.return_value = {
        "DBClusterSnapshot": {"Status": "creating"}
    }
    mock_rds_for.return_value = rds
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


@patch("mcp_servers.operations.tools.create_snapshot.rds_client_for_cluster")
@patch("mcp_servers.operations.tools.create_snapshot.verify_approval")
def test_auto_generates_id_when_omitted(mock_guard, mock_rds_for):
    mock_guard.return_value = {"ok": True}
    rds = MagicMock()
    rds.create_db_cluster_snapshot.return_value = {"DBClusterSnapshot": {"Status": "creating"}}
    mock_rds_for.return_value = rds
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


@patch("mcp_servers.operations.tools.create_snapshot.rds_client_for_cluster")
@patch("mcp_servers.operations.tools.create_snapshot.verify_approval")
def test_create_failure_surfaced_without_exception_text(mock_guard, mock_rds_for):
    """create_failed keeps its status, but the reason is STATIC: a real RDS error
    names the hub account, the platform role and the cluster ARN."""
    mock_guard.return_value = {"ok": True}
    rds = MagicMock()
    rds.create_db_cluster_snapshot.side_effect = RuntimeError(
        "User: arn:aws:sts::123456789012:assumed-role/dbops-dev-mcp-role/x is not authorized"
    )
    mock_rds_for.return_value = rds
    cache = MagicMock()
    out = create_snapshot_impl(
        cache, cluster_id="prod-pg-1", approved=True, approval_id="aid-1"
    )
    assert out["status"] == "create_failed"
    assert out["cluster_id"] == "prod-pg-1"
    assert "123456789012" not in out["error"]
    assert "assumed-role" not in out["error"]
    assert "스냅샷 생성 요청이 실패했습니다" in out["error"]


@patch("mcp_servers.operations.tools.create_snapshot.rds_client_for_cluster")
@patch("mcp_servers.operations.tools.create_snapshot.verify_approval")
def test_create_failure_keeps_the_aws_error_code(mock_guard, mock_rds_for):
    """The CODE is a bounded enum and is the actionable part, so it stays."""
    mock_guard.return_value = {"ok": True}
    rds = MagicMock()
    rds.create_db_cluster_snapshot.side_effect = ClientError(
        {"Error": {"Code": "DBClusterSnapshotAlreadyExistsFault",
                   "Message": "snapshot arn:aws:rds:ap-northeast-2:123456789012:x exists"}},
        "CreateDBClusterSnapshot",
    )
    mock_rds_for.return_value = rds
    out = create_snapshot_impl(
        MagicMock(), cluster_id="prod-pg-1", approved=True, approval_id="aid-1"
    )
    assert out["status"] == "create_failed"
    assert "DBClusterSnapshotAlreadyExistsFault" in out["error"]
    assert "123456789012" not in out["error"]


def test_make_snapshot_id_is_valid():
    import re

    sid = _make_snapshot_id("dbops-dev-sample-samplepg789869c8-caf4ladtqz0i")
    assert re.match(r"^[a-zA-Z][a-zA-Z0-9]*(-[a-zA-Z0-9]+)*$", sid)
    assert len(sid) <= 63
    assert "--" not in sid
    assert not sid.endswith("-")
