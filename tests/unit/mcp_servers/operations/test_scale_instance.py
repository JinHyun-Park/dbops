"""Tests for the Aurora reader scale-out/scale-in write tools (N-③), approval-gated.

Covers for BOTH tools: deny-by-default (preview → approval_required, no RDS
write), payload-hash binding via the REAL guard (mismatch → no create/delete),
and the execute path. add: writer-class defaulting, create args. remove: the
writer/last-instance/not-found protections and the delete path. Failure paths
return a friendly status with no str(e) leak.

The handler engine-gate (unsupported_engine on non-relational) is exercised in
test_operations_engine_gate.py; here we test the impls assuming the gate passed.
Every describe_* MagicMock returns a real dict so a paginator/loop can't hang.
"""

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.add_reader_instance import add_reader_instance_impl
from mcp_servers.operations.tools.remove_reader_instance import remove_reader_instance_impl
from mcp_servers.shared.approval_guard import canonical_action_hash

_A = "mcp_servers.operations.tools.add_reader_instance"
_R = "mcp_servers.operations.tools.remove_reader_instance"
_MTP = "mcp_servers.shared.managed_tag_preflight"


def _rds_cluster(members, cluster_id="prod-pg-1", engine="aurora-postgresql", instances=None):
    """An RDS MagicMock whose describe_* return real dicts (never bare mocks —
    a bare MagicMock in a paginate/iter loop hangs)."""
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {
        "DBClusters": [{
            "DBClusterIdentifier": cluster_id,
            "Engine": engine,
            "DBClusterMembers": members,
        }]
    }
    rds.describe_db_instances.return_value = {"DBInstances": instances or []}
    rds.create_db_instance.return_value = {"DBInstance": {"DBInstanceStatus": "creating"}}
    rds.delete_db_instance.return_value = {"DBInstance": {"DBInstanceStatus": "deleting"}}
    return rds


# ───────────────────────── add (scale-out) ─────────────────────────

def test_add_requires_new_instance_id():
    out = add_reader_instance_impl(MagicMock(), cluster_id="prod-pg-1")
    assert out["status"] == "invalid_instance"


@patch(f"{_A}.client_for_cluster")
def test_add_preview_resolves_writer_class(mock_client):
    """PREVIEW with empty class resolves the WRITER's concrete class so the DBA
    approves the actual billable class; the returned approval_required and
    cli_preview carry that concrete class, and nothing is created."""
    rds = _rds_cluster(
        members=[{"DBInstanceIdentifier": "w", "IsClusterWriter": True}],
        instances=[{"DBInstanceIdentifier": "w", "DBInstanceClass": "db.serverless"}],
    )
    mock_client.return_value = rds
    out = add_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", new_instance_id="r-new",
    )
    assert out["status"] == "approval_required"
    assert out["new_instance_id"] == "r-new"
    assert out["instance_class"] == "db.serverless"  # concrete, resolved in preview
    assert "db.serverless" in out["cli_preview"]
    rds.create_db_instance.assert_not_called()


@patch(f"{_A}.client_for_cluster")
def test_add_preview_unresolvable_class_asks(mock_client):
    """PREVIEW where the writer class can't be resolved → needs_instance_class,
    NOT an approval_required carrying an empty class."""
    rds = _rds_cluster(
        members=[{"DBInstanceIdentifier": "w", "IsClusterWriter": True}],
        instances=[],  # describe_db_instances yields nothing → unresolvable
    )
    mock_client.return_value = rds
    out = add_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", new_instance_id="r-new",
    )
    assert out["status"] == "needs_instance_class"
    rds.create_db_instance.assert_not_called()


@patch(f"{_A}.verify_approval")
@patch(f"{_A}.client_for_cluster")
def test_add_execute_requires_bound_class(mock_client, mock_guard):
    """Approved but instance_class empty (never bound at approval) → add_failed
    and NO create — execute never resolves a class the DBA didn't approve."""
    rds = _rds_cluster([{"DBInstanceIdentifier": "w", "IsClusterWriter": True}])
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = add_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", new_instance_id="r-new",
        approved=True, approval_id="aid-1",
    )
    assert out["status"] == "add_failed"
    rds.create_db_instance.assert_not_called()


@patch(f"{_A}.verify_approval")
@patch(f"{_A}.client_for_cluster")
def test_add_execute_uses_approved_class(mock_client, mock_guard):
    """Approved with a concrete class → create_db_instance with that EXACT class;
    execute does NO post-approval writer-class lookup."""
    rds = _rds_cluster([{"DBInstanceIdentifier": "w", "IsClusterWriter": True}])
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = add_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", new_instance_id="r-new",
        instance_class="db.r6g.large", approved=True, approval_id="aid-1",
    )
    assert out["status"] == "instance_added"
    assert out["instance_class"] == "db.r6g.large"
    call = rds.create_db_instance.call_args.kwargs
    assert call["DBInstanceIdentifier"] == "r-new"
    assert call["DBClusterIdentifier"] == "prod-pg-1"
    assert call["Engine"] == "aurora-postgresql"
    assert call["DBInstanceClass"] == "db.r6g.large"
    assert call["Tags"] == [{"Key": "dbops:managed", "Value": "scale-out"}]
    assert "AvailabilityZone" not in call  # none passed
    rds.describe_db_instances.assert_not_called()  # no post-approval class lookup


@patch(f"{_A}.verify_approval")
@patch(f"{_A}.client_for_cluster")
def test_add_explicit_class_and_az(mock_client, mock_guard):
    rds = _rds_cluster([{"DBInstanceIdentifier": "w", "IsClusterWriter": True}])
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = add_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", new_instance_id="r-new",
        instance_class="db.serverless", availability_zone="ap-northeast-2a",
        approved=True, approval_id="aid-1",
    )
    assert out["status"] == "instance_added"
    assert out["availability_zone"] == "ap-northeast-2a"
    call = rds.create_db_instance.call_args.kwargs
    assert call["DBInstanceClass"] == "db.serverless"
    assert call["AvailabilityZone"] == "ap-northeast-2a"
    # writer-class lookup is skipped when the class is explicit
    rds.describe_db_instances.assert_not_called()


@patch(f"{_A}.verify_approval")
@patch(f"{_A}.client_for_cluster")
def test_add_failure_returns_friendly_no_leak(mock_client, mock_guard):
    rds = _rds_cluster([{"DBInstanceIdentifier": "w", "IsClusterWriter": True}])
    rds.create_db_instance.side_effect = Exception("SECRET_INTERNAL_LEAK")
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = add_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", new_instance_id="r-new",
        instance_class="db.serverless", approved=True, approval_id="aid-1",
    )
    assert out["status"] == "add_failed"
    assert "SECRET_INTERNAL_LEAK" not in json.dumps(out)


@patch(f"{_A}.client_for_cluster")
def test_add_payload_hash_mismatch_rejected(mock_client):
    """A real approval minted for class '' cannot be consumed to add the reader
    with a different class — the guard's payload_hash refuses it, no create."""
    rds = _rds_cluster([{"DBInstanceIdentifier": "w", "IsClusterWriter": True}])
    mock_client.return_value = rds
    row = {
        "approval_id": "aid-1", "created_at": "1", "approval_status": "approved",
        "cluster_id": "prod-pg-1", "action_type": "add_reader_instance",
        "payload_hash": canonical_action_hash("add_reader_instance", {
            "cluster_id": "prod-pg-1", "new_instance_id": "r-new",
            "instance_class": "", "availability_zone": "",
        }),
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    table = MagicMock()
    table.scan.return_value = {"Items": [row]}
    resource = MagicMock()
    resource.Table.return_value = table
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}, clear=False), \
         patch("mcp_servers.shared.approval_guard.boto3.resource", return_value=resource):
        out = add_reader_instance_impl(
            MagicMock(), cluster_id="prod-pg-1", new_instance_id="r-new",
            instance_class="db.big", approved=True, approval_id="aid-1",
        )
    assert out["status"] == "approval_denied"
    rds.create_db_instance.assert_not_called()
    table.update_item.assert_not_called()  # never consumed


# ───────────────────────── remove (scale-in) ─────────────────────────

def test_remove_requires_instance_id():
    out = remove_reader_instance_impl(MagicMock(), cluster_id="prod-pg-1")
    assert out["status"] == "invalid_instance"


@patch(f"{_R}.client_for_cluster")
def test_remove_requires_approval(mock_client):
    out = remove_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", instance_id="r1",
    )
    assert out["status"] == "approval_required"
    assert "cli_preview" in out
    # preview returns before the RDS client is even used
    mock_client.assert_not_called()


@patch(f"{_MTP}.is_cross_account", return_value=True)
@patch(f"{_R}.is_cross_account", return_value=True)
@patch(f"{_R}.client_for_cluster")
def test_remove_preview_warns_when_the_INSTANCE_lacks_ManagedBy(mock_client, _xa, _xa2):
    """The other side of the invariant above.

    A same-account preview resolves no client (asserted above). A spoke-account
    one does, because rds:DeleteDBInstance is gated on `ManagedBy=dbops` on the
    INSTANCE and that denial would otherwise land after the single-use approval
    was already consumed. Note the describe is of the INSTANCE, not the cluster:
    the cluster's tags answer a different question than IAM asks here.
    """
    rds = MagicMock()
    rds.describe_db_instances.return_value = {
        "DBInstances": [{"DBInstanceArn": "arn:aws:rds:::db:r1"}]
    }
    rds.list_tags_for_resource.return_value = {
        "TagList": [{"Key": "dbops-demo", "Value": "true"}]  # no ManagedBy
    }
    mock_client.return_value = rds

    out = remove_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", instance_id="r1",
    )
    assert out["status"] == "approval_required"
    assert "rds:DeleteDBInstance" in out["warning"]
    rds.describe_db_instances.assert_called_once_with(DBInstanceIdentifier="r1")
    rds.describe_db_clusters.assert_not_called()


@patch(f"{_MTP}.is_cross_account", return_value=True)
@patch(f"{_R}.is_cross_account", return_value=True)
@patch(f"{_R}.client_for_cluster")
def test_remove_preview_stays_silent_when_the_instance_is_tagged(mock_client, _xa, _xa2):
    rds = MagicMock()
    rds.describe_db_instances.return_value = {
        "DBInstances": [{"DBInstanceArn": "arn:x"}]
    }
    rds.list_tags_for_resource.return_value = {
        "TagList": [{"Key": "ManagedBy", "Value": "dbops"}]
    }
    mock_client.return_value = rds

    out = remove_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", instance_id="r1",
    )
    assert "warning" not in out


@patch(f"{_MTP}.is_cross_account", return_value=True)
@patch(f"{_R}.is_cross_account", return_value=True)
@patch(f"{_R}.client_for_cluster", side_effect=RuntimeError("assume-role failed"))
def test_remove_preview_survives_a_broken_preflight(_client, _xa, _xa2):
    """Fail-open: the preflight is a nicety and must never turn a working preview
    into an error, so a client/STS failure yields a card with no warning."""
    out = remove_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", instance_id="r1",
    )
    assert out["status"] == "approval_required"
    assert "warning" not in out


@patch(f"{_R}.verify_approval")
@patch(f"{_R}.client_for_cluster")
def test_remove_protects_writer(mock_client, mock_guard):
    rds = _rds_cluster([
        {"DBInstanceIdentifier": "w", "IsClusterWriter": True},
        {"DBInstanceIdentifier": "r1", "IsClusterWriter": False},
    ])
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = remove_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", instance_id="w",
        approved=True, approval_id="aid-1",
    )
    assert out["status"] == "cannot_remove_writer"
    rds.delete_db_instance.assert_not_called()


@patch(f"{_R}.verify_approval")
@patch(f"{_R}.client_for_cluster")
def test_remove_not_found(mock_client, mock_guard):
    rds = _rds_cluster([
        {"DBInstanceIdentifier": "w", "IsClusterWriter": True},
        {"DBInstanceIdentifier": "r1", "IsClusterWriter": False},
    ])
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = remove_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", instance_id="ghost",
        approved=True, approval_id="aid-1",
    )
    assert out["status"] == "instance_not_found"
    rds.delete_db_instance.assert_not_called()


@patch(f"{_R}.verify_approval")
@patch(f"{_R}.client_for_cluster")
def test_remove_protects_last_instance(mock_client, mock_guard):
    """Even a reader must not be deleted if it is the cluster's ONLY instance —
    never leave a cluster with 0 instances."""
    rds = _rds_cluster([{"DBInstanceIdentifier": "r-only", "IsClusterWriter": False}])
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = remove_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", instance_id="r-only",
        approved=True, approval_id="aid-1",
    )
    assert out["status"] == "cannot_remove_last_instance"
    rds.delete_db_instance.assert_not_called()


@patch(f"{_R}.verify_approval")
@patch(f"{_R}.client_for_cluster")
def test_remove_executes_when_approved(mock_client, mock_guard):
    rds = _rds_cluster([
        {"DBInstanceIdentifier": "w", "IsClusterWriter": True},
        {"DBInstanceIdentifier": "r1", "IsClusterWriter": False},
    ])
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = remove_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", instance_id="r1",
        approved=True, approval_id="aid-1",
    )
    assert out["status"] == "instance_removing"
    rds.delete_db_instance.assert_called_once_with(DBInstanceIdentifier="r1")


@patch(f"{_R}.verify_approval")
@patch(f"{_R}.client_for_cluster")
def test_remove_failure_returns_friendly_no_leak(mock_client, mock_guard):
    rds = _rds_cluster([
        {"DBInstanceIdentifier": "w", "IsClusterWriter": True},
        {"DBInstanceIdentifier": "r1", "IsClusterWriter": False},
    ])
    rds.delete_db_instance.side_effect = Exception("SECRET_INTERNAL_LEAK")
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = remove_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", instance_id="r1",
        approved=True, approval_id="aid-1",
    )
    assert out["status"] == "remove_failed"
    assert "SECRET_INTERNAL_LEAK" not in json.dumps(out)


@patch(f"{_R}.verify_approval")
@patch(f"{_R}.client_for_cluster")
def test_remove_rechecks_writer_before_delete_toctou(mock_client, mock_guard):
    """A failover between the early guard and the delete promotes the target to
    writer: describe returns it as a READER first (early guards pass) then as the
    WRITER on the pre-delete re-check → refuse to delete."""
    rds = _rds_cluster([])  # member list supplied via side_effect below
    reader_view = {"DBClusters": [{
        "DBClusterIdentifier": "prod-pg-1", "Engine": "aurora-postgresql",
        "DBClusterMembers": [
            {"DBInstanceIdentifier": "w", "IsClusterWriter": True},
            {"DBInstanceIdentifier": "r1", "IsClusterWriter": False},
        ],
    }]}
    writer_view = {"DBClusters": [{
        "DBClusterIdentifier": "prod-pg-1", "Engine": "aurora-postgresql",
        "DBClusterMembers": [
            {"DBInstanceIdentifier": "w", "IsClusterWriter": False},
            {"DBInstanceIdentifier": "r1", "IsClusterWriter": True},  # promoted
        ],
    }]}
    rds.describe_db_clusters.side_effect = [reader_view, writer_view]
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = remove_reader_instance_impl(
        MagicMock(), cluster_id="prod-pg-1", instance_id="r1",
        approved=True, approval_id="aid-1",
    )
    assert out["status"] == "cannot_remove_writer"
    rds.delete_db_instance.assert_not_called()


@patch(f"{_R}.client_for_cluster")
def test_remove_payload_hash_mismatch_rejected(mock_client):
    """A real approval minted for r1 cannot be consumed to delete r2 — the
    guard's payload_hash refuses it, no delete, no consume."""
    rds = _rds_cluster([
        {"DBInstanceIdentifier": "w", "IsClusterWriter": True},
        {"DBInstanceIdentifier": "r1", "IsClusterWriter": False},
        {"DBInstanceIdentifier": "r2", "IsClusterWriter": False},
    ])
    mock_client.return_value = rds
    row = {
        "approval_id": "aid-1", "created_at": "1", "approval_status": "approved",
        "cluster_id": "prod-pg-1", "action_type": "remove_reader_instance",
        "payload_hash": canonical_action_hash("remove_reader_instance", {
            "cluster_id": "prod-pg-1", "instance_id": "r1",
        }),
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    table = MagicMock()
    table.scan.return_value = {"Items": [row]}
    resource = MagicMock()
    resource.Table.return_value = table
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}, clear=False), \
         patch("mcp_servers.shared.approval_guard.boto3.resource", return_value=resource):
        out = remove_reader_instance_impl(
            MagicMock(), cluster_id="prod-pg-1", instance_id="r2",
            approved=True, approval_id="aid-1",
        )
    assert out["status"] == "approval_denied"
    rds.delete_db_instance.assert_not_called()
    table.update_item.assert_not_called()
