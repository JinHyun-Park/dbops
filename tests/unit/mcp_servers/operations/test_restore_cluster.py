"""Tests for the agent-side restore_cluster MCP tool (approval-gated, high risk)."""

import json
from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.restore_cluster import restore_cluster_impl

# A realistic RDS error: it carries the hub account id, the platform role name and
# the target ARN. All of it belongs in CloudWatch, none of it in a tool response.
_LEAKY = (
    "An error occurred (AccessDenied) when calling the RestoreDBClusterFromSnapshot "
    "operation: User: arn:aws:sts::123456789012:assumed-role/dbops-dev-operations-role/"
    "dbops-dev-operations is not authorized to perform: rds:RestoreDBClusterFromSnapshot "
    "on resource: arn:aws:rds:ap-northeast-2:123456789012:cluster-snapshot:snap-1"
)
_LEAK_TOKENS = ("123456789012", "dbops-dev-operations-role", "AccessDenied", "arn:aws")


def _assert_no_leak(out):
    """No RESPONSE field may carry exception text (project-wide rule)."""
    blob = json.dumps(out, ensure_ascii=False, default=str)
    for token in _LEAK_TOKENS:
        assert token not in blob, f"exception text leaked into response: {token!r} in {blob!r}"
    assert "error" not in out, "raw exception field must be gone"


def _rds_mock():
    rds = MagicMock()
    rds.exceptions.DBClusterAlreadyExistsFault = type(
        "DBClusterAlreadyExistsFault", (Exception,), {}
    )
    rds.describe_db_clusters.return_value = {
        "DBClusters": [{
            "Engine": "aurora-postgresql",
            "DBSubnetGroup": "subnet-grp",
            "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-1"}],
            "ServerlessV2ScalingConfiguration": {"MinCapacity": 0.5, "MaxCapacity": 4},
        }]
    }
    rds.restore_db_cluster_from_snapshot.return_value = {
        "DBCluster": {
            "Status": "creating",
            "DBClusterArn": "arn:aws:rds:ap-northeast-2:123456789012:cluster:restored-1",
        }
    }
    rds.restore_db_cluster_to_point_in_time.return_value = {
        "DBCluster": {
            "Status": "creating",
            "DBClusterArn": "arn:aws:rds:ap-northeast-2:123456789012:cluster:restored-1",
        }
    }
    return rds


def test_requires_approval_without_approved_flag():
    out = restore_cluster_impl(MagicMock(), cluster_id="prod-pg-1", new_cluster_id="restored-1")
    assert out["status"] == "approval_required"


@patch("mcp_servers.operations.tools.restore_cluster.verify_approval")
def test_approval_denied_when_guard_rejects(mock_guard):
    mock_guard.return_value = {"ok": False, "reason": "stale"}
    out = restore_cluster_impl(
        MagicMock(), cluster_id="prod-pg-1", new_cluster_id="restored-1",
        approved=True, approval_id="aid",
    )
    assert out["status"] == "approval_denied"
    assert "stale" in out["reason"]
    mock_guard.assert_called_once_with(
        "aid",
        "prod-pg-1",
        "restore_cluster",
        payload={
            "new_cluster_id": "restored-1",
            "mode": "snapshot",
            "snapshot_id": "",
            "restore_to_time": "",
            "use_latest": False,
        },
    )


@patch("mcp_servers.operations.tools.restore_cluster.verify_approval")
def test_rejects_new_equal_to_source(mock_guard):
    mock_guard.return_value = {"ok": True}
    out = restore_cluster_impl(
        MagicMock(), cluster_id="prod-pg-1", new_cluster_id="prod-pg-1",
        approved=True, approval_id="aid",
    )
    assert out["status"] == "invalid_new_cluster_id"
    assert "differ" in out["reason"]


@patch("mcp_servers.operations.tools.restore_cluster.verify_approval")
def test_rejects_invalid_new_cluster_id(mock_guard):
    mock_guard.return_value = {"ok": True}
    out = restore_cluster_impl(
        MagicMock(), cluster_id="prod-pg-1", new_cluster_id="-bad--id-",
        approved=True, approval_id="aid",
    )
    assert out["status"] == "invalid_new_cluster_id"


@patch("mcp_servers.operations.tools.restore_cluster.rds_client_for_cluster")
@patch("mcp_servers.operations.tools.restore_cluster.verify_approval")
def test_snapshot_restore_when_approved(mock_guard, mock_rds_for):
    mock_guard.return_value = {"ok": True}
    rds = _rds_mock()
    mock_rds_for.return_value = rds
    out = restore_cluster_impl(
        MagicMock(), cluster_id="prod-pg-1", new_cluster_id="restored-1",
        mode="snapshot", snapshot_id="snap-1", approved=True, approval_id="aid",
    )
    assert out["status"] == "restoring"
    assert out["new_cluster_id"] == "restored-1"
    assert out["restore_source"] == "snapshot:snap-1"
    call = rds.restore_db_cluster_from_snapshot.call_args.kwargs
    assert call["DBClusterIdentifier"] == "restored-1"
    assert call["SnapshotIdentifier"] == "snap-1"
    # network/scaling config cloned from the source
    assert call["DBSubnetGroupName"] == "subnet-grp"
    assert call["VpcSecurityGroupIds"] == ["sg-1"]


@patch("mcp_servers.operations.tools.restore_cluster.rds_client_for_cluster")
@patch("mcp_servers.operations.tools.restore_cluster.verify_approval")
def test_snapshot_mode_requires_snapshot_id(mock_guard, mock_rds_for):
    mock_guard.return_value = {"ok": True}
    mock_rds_for.return_value = _rds_mock()
    out = restore_cluster_impl(
        MagicMock(), cluster_id="prod-pg-1", new_cluster_id="restored-1",
        mode="snapshot", approved=True, approval_id="aid",
    )
    assert out["status"] == "invalid_request"


@patch("mcp_servers.operations.tools.restore_cluster.rds_client_for_cluster")
@patch("mcp_servers.operations.tools.restore_cluster.verify_approval")
def test_pitr_restore_use_latest(mock_guard, mock_rds_for):
    mock_guard.return_value = {"ok": True}
    rds = _rds_mock()
    mock_rds_for.return_value = rds
    out = restore_cluster_impl(
        MagicMock(), cluster_id="prod-pg-1", new_cluster_id="restored-1",
        mode="pitr", use_latest=True, approved=True, approval_id="aid",
    )
    assert out["status"] == "restoring"
    assert out["restore_source"] == "pitr:latest"
    call = rds.restore_db_cluster_to_point_in_time.call_args.kwargs
    assert call["SourceDBClusterIdentifier"] == "prod-pg-1"
    assert call["UseLatestRestorableTime"] is True


@patch("mcp_servers.operations.tools.restore_cluster.rds_client_for_cluster")
@patch("mcp_servers.operations.tools.restore_cluster.verify_approval")
def test_pitr_requires_time_or_latest(mock_guard, mock_rds_for):
    mock_guard.return_value = {"ok": True}
    mock_rds_for.return_value = _rds_mock()
    out = restore_cluster_impl(
        MagicMock(), cluster_id="prod-pg-1", new_cluster_id="restored-1",
        mode="pitr", approved=True, approval_id="aid",
    )
    assert out["status"] == "invalid_request"


@patch("mcp_servers.operations.tools.restore_cluster.rds_client_for_cluster")
@patch("mcp_servers.operations.tools.restore_cluster.verify_approval")
def test_already_exists_surfaced(mock_guard, mock_rds_for):
    mock_guard.return_value = {"ok": True}
    rds = _rds_mock()
    rds.restore_db_cluster_from_snapshot.side_effect = (
        rds.exceptions.DBClusterAlreadyExistsFault()
    )
    mock_rds_for.return_value = rds
    out = restore_cluster_impl(
        MagicMock(), cluster_id="prod-pg-1", new_cluster_id="restored-1",
        mode="snapshot", snapshot_id="snap-1", approved=True, approval_id="aid",
    )
    assert out["status"] == "already_exists"


@patch("mcp_servers.operations.tools.restore_cluster.rds_client_for_cluster")
@patch("mcp_servers.operations.tools.restore_cluster.verify_approval")
def test_restore_failure_surfaced(mock_guard, mock_rds_for):
    """The failure is still reported, but with a STATIC reason: the RDS exception
    (hub account id, platform role name, snapshot ARN) never enters the response."""
    mock_guard.return_value = {"ok": True}
    rds = _rds_mock()
    rds.restore_db_cluster_from_snapshot.side_effect = RuntimeError(_LEAKY)
    mock_rds_for.return_value = rds
    out = restore_cluster_impl(
        MagicMock(), cluster_id="prod-pg-1", new_cluster_id="restored-1",
        mode="snapshot", snapshot_id="snap-1", approved=True, approval_id="aid",
    )
    assert out["status"] == "restore_failed"
    _assert_no_leak(out)
    # caller inputs are what the DBA needs to retry, and they are safe to echo
    assert out["new_cluster_id"] == "restored-1"
    assert out["mode"] == "snapshot"
    assert out["reason"]
