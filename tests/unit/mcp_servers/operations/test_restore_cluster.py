"""Tests for the agent-side restore_cluster MCP tool (approval-gated, high risk)."""

from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.restore_cluster import restore_cluster_impl


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


@patch("mcp_servers.operations.tools.restore_cluster.boto3")
@patch("mcp_servers.operations.tools.restore_cluster.verify_approval")
def test_snapshot_restore_when_approved(mock_guard, mock_boto3):
    mock_guard.return_value = {"ok": True}
    rds = _rds_mock()
    mock_boto3.client.return_value = rds
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


@patch("mcp_servers.operations.tools.restore_cluster.boto3")
@patch("mcp_servers.operations.tools.restore_cluster.verify_approval")
def test_snapshot_mode_requires_snapshot_id(mock_guard, mock_boto3):
    mock_guard.return_value = {"ok": True}
    mock_boto3.client.return_value = _rds_mock()
    out = restore_cluster_impl(
        MagicMock(), cluster_id="prod-pg-1", new_cluster_id="restored-1",
        mode="snapshot", approved=True, approval_id="aid",
    )
    assert out["status"] == "invalid_request"


@patch("mcp_servers.operations.tools.restore_cluster.boto3")
@patch("mcp_servers.operations.tools.restore_cluster.verify_approval")
def test_pitr_restore_use_latest(mock_guard, mock_boto3):
    mock_guard.return_value = {"ok": True}
    rds = _rds_mock()
    mock_boto3.client.return_value = rds
    out = restore_cluster_impl(
        MagicMock(), cluster_id="prod-pg-1", new_cluster_id="restored-1",
        mode="pitr", use_latest=True, approved=True, approval_id="aid",
    )
    assert out["status"] == "restoring"
    assert out["restore_source"] == "pitr:latest"
    call = rds.restore_db_cluster_to_point_in_time.call_args.kwargs
    assert call["SourceDBClusterIdentifier"] == "prod-pg-1"
    assert call["UseLatestRestorableTime"] is True


@patch("mcp_servers.operations.tools.restore_cluster.boto3")
@patch("mcp_servers.operations.tools.restore_cluster.verify_approval")
def test_pitr_requires_time_or_latest(mock_guard, mock_boto3):
    mock_guard.return_value = {"ok": True}
    mock_boto3.client.return_value = _rds_mock()
    out = restore_cluster_impl(
        MagicMock(), cluster_id="prod-pg-1", new_cluster_id="restored-1",
        mode="pitr", approved=True, approval_id="aid",
    )
    assert out["status"] == "invalid_request"


@patch("mcp_servers.operations.tools.restore_cluster.boto3")
@patch("mcp_servers.operations.tools.restore_cluster.verify_approval")
def test_already_exists_surfaced(mock_guard, mock_boto3):
    mock_guard.return_value = {"ok": True}
    rds = _rds_mock()
    rds.restore_db_cluster_from_snapshot.side_effect = (
        rds.exceptions.DBClusterAlreadyExistsFault()
    )
    mock_boto3.client.return_value = rds
    out = restore_cluster_impl(
        MagicMock(), cluster_id="prod-pg-1", new_cluster_id="restored-1",
        mode="snapshot", snapshot_id="snap-1", approved=True, approval_id="aid",
    )
    assert out["status"] == "already_exists"


@patch("mcp_servers.operations.tools.restore_cluster.boto3")
@patch("mcp_servers.operations.tools.restore_cluster.verify_approval")
def test_restore_failure_surfaced(mock_guard, mock_boto3):
    mock_guard.return_value = {"ok": True}
    rds = _rds_mock()
    rds.restore_db_cluster_from_snapshot.side_effect = RuntimeError("boom")
    mock_boto3.client.return_value = rds
    out = restore_cluster_impl(
        MagicMock(), cluster_id="prod-pg-1", new_cluster_id="restored-1",
        mode="snapshot", snapshot_id="snap-1", approved=True, approval_id="aid",
    )
    assert out["status"] == "restore_failed"
    assert "boom" in out["error"]
