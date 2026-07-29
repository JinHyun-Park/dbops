"""Tests for the 3 approval-gated RDS-instance write tools (R-3): reboot,
snapshot, modify-instance-class.

Covers per tool: preview binds the resolved values into the approval_required
payload; not_applicable pre-checks skip approval; approved + verify_approval
mocked ok → executes with the right AWS call args; TOCTOU drift → refuses WITHOUT
calling the mutating API; no str(e) leak on failure. Plus the allowlist/_project
round-trip for the 3 new action_types (a real approval minted for one payload
cannot be redirected to a different one — the guard's payload_hash refuses it).

The handler engine-gate (unsupported_engine on non-rds_instance) is exercised in
test_operations_engine_gate.py. Every describe_* MagicMock returns a real dict so
a paginate/iter loop can't hang.
"""

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.create_rds_snapshot import create_rds_snapshot_impl
from mcp_servers.operations.tools.modify_rds_instance_class import modify_rds_instance_class_impl
from mcp_servers.operations.tools.reboot_rds_instance import reboot_rds_instance_impl
from mcp_servers.shared.approval_guard import canonical_action_hash

_RB = "mcp_servers.operations.tools.reboot_rds_instance"
_SN = "mcp_servers.operations.tools.create_rds_snapshot"
_MC = "mcp_servers.operations.tools.modify_rds_instance_class"


def _rds_instance(status="available", instance_class="db.t3.medium", cluster_member=False):
    """An RDS MagicMock whose describe_db_instances returns a real dict for a
    standalone (non-Aurora-member) instance unless cluster_member=True."""
    inst = {"DBInstanceIdentifier": "rds-mysql-1",
            "DBInstanceStatus": status, "DBInstanceClass": instance_class}
    if cluster_member:
        inst["DBClusterIdentifier"] = "some-aurora-cluster"
    rds = MagicMock()
    rds.describe_db_instances.return_value = {"DBInstances": [inst]}
    rds.reboot_db_instance.return_value = {"DBInstance": {"DBInstanceStatus": "rebooting"}}
    rds.create_db_snapshot.return_value = {"DBSnapshot": {"Status": "creating"}}
    rds.modify_db_instance.return_value = {"DBInstance": {"DBInstanceStatus": "modifying"}}
    return rds


def _approval_row(action_type, details):
    return {
        "approval_id": "aid-1", "created_at": "1", "approval_status": "approved",
        "cluster_id": "rds-mysql-1", "action_type": action_type,
        "payload_hash": canonical_action_hash(action_type, details),
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }


def _guarded_table(row):
    table = MagicMock()
    table.scan.return_value = {"Items": [row]}
    resource = MagicMock()
    resource.Table.return_value = table
    return table, resource


# ───────────────────────── reboot_rds_instance ─────────────────────────

@patch(f"{_RB}.client_for_cluster")
def test_reboot_preview_binds_cluster_id(mock_client):
    """PREVIEW on an available standalone instance → approval_required binding
    cluster_id; nothing rebooted."""
    rds = _rds_instance(status="available")
    mock_client.return_value = rds
    out = reboot_rds_instance_impl(MagicMock(), cluster_id="rds-mysql-1")
    assert out["status"] == "approval_required"
    assert out["cluster_id"] == "rds-mysql-1"
    assert "cli_preview" in out
    rds.reboot_db_instance.assert_not_called()


@patch(f"{_RB}.client_for_cluster")
def test_reboot_not_available_not_applicable(mock_client):
    rds = _rds_instance(status="modifying")
    mock_client.return_value = rds
    out = reboot_rds_instance_impl(MagicMock(), cluster_id="rds-mysql-1")
    assert out["status"] == "not_applicable"
    rds.reboot_db_instance.assert_not_called()


@patch(f"{_RB}.client_for_cluster")
def test_reboot_aurora_member_unsupported(mock_client):
    """A cluster-member instance must NOT be rebooted through the instance tool
    (Aurora reboots go through cluster/reader tooling)."""
    rds = _rds_instance(status="available", cluster_member=True)
    mock_client.return_value = rds
    out = reboot_rds_instance_impl(MagicMock(), cluster_id="rds-mysql-1")
    assert out["status"] == "unsupported"
    rds.reboot_db_instance.assert_not_called()


@patch(f"{_RB}.verify_approval")
@patch(f"{_RB}.client_for_cluster")
def test_reboot_execute_when_approved(mock_client, mock_guard):
    rds = _rds_instance(status="available")
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = reboot_rds_instance_impl(
        MagicMock(), cluster_id="rds-mysql-1", approved=True, approval_id="aid-1")
    assert out["status"] == "rebooting"
    rds.reboot_db_instance.assert_called_once_with(DBInstanceIdentifier="rds-mysql-1")


@patch(f"{_RB}.verify_approval")
@patch(f"{_RB}.client_for_cluster")
def test_reboot_toctou_not_available_refuses(mock_client, mock_guard):
    """State drifted to non-available since approval → refuse, no reboot."""
    rds = _rds_instance(status="stopped")
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = reboot_rds_instance_impl(
        MagicMock(), cluster_id="rds-mysql-1", approved=True, approval_id="aid-1")
    assert out["status"] == "not_applicable"
    rds.reboot_db_instance.assert_not_called()


@patch(f"{_RB}.verify_approval")
@patch(f"{_RB}.client_for_cluster")
def test_reboot_failure_no_leak(mock_client, mock_guard):
    rds = _rds_instance(status="available")
    rds.reboot_db_instance.side_effect = Exception("SECRET_INTERNAL_LEAK")
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = reboot_rds_instance_impl(
        MagicMock(), cluster_id="rds-mysql-1", approved=True, approval_id="aid-1")
    assert out["status"] == "reboot_failed"
    assert "SECRET_INTERNAL_LEAK" not in json.dumps(out)


@patch(f"{_RB}.client_for_cluster")
def test_reboot_payload_hash_mismatch_rejected(mock_client):
    """A real approval minted for cluster A cannot drive a reboot of cluster B."""
    rds = _rds_instance(status="available")
    mock_client.return_value = rds
    row = _approval_row("reboot_rds_instance", {"cluster_id": "other-instance"})
    table, resource = _guarded_table(row)
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}, clear=False), \
         patch("mcp_servers.shared.approval_guard.boto3.resource", return_value=resource):
        out = reboot_rds_instance_impl(
            MagicMock(), cluster_id="rds-mysql-1", approved=True, approval_id="aid-1")
    assert out["status"] == "approval_denied"
    rds.reboot_db_instance.assert_not_called()
    table.update_item.assert_not_called()


# ───────────────────────── create_rds_snapshot ─────────────────────────

@patch(f"{_SN}.client_for_cluster")
def test_snapshot_preview_resolves_and_binds_id(mock_client):
    """PREVIEW with empty snapshot_id resolves a concrete default NOW and binds
    it into approval_required; nothing created."""
    rds = _rds_instance(status="available")
    mock_client.return_value = rds
    out = create_rds_snapshot_impl(MagicMock(), cluster_id="rds-mysql-1")
    assert out["status"] == "approval_required"
    assert out["snapshot_id"].startswith("dbops-rds-mysql-1-")
    rds.create_db_snapshot.assert_not_called()


@patch(f"{_SN}.client_for_cluster")
def test_snapshot_not_available_not_applicable(mock_client):
    rds = _rds_instance(status="stopped")
    mock_client.return_value = rds
    out = create_rds_snapshot_impl(MagicMock(), cluster_id="rds-mysql-1")
    assert out["status"] == "not_applicable"
    rds.create_db_snapshot.assert_not_called()


@patch(f"{_SN}.verify_approval")
@patch(f"{_SN}.client_for_cluster")
def test_snapshot_execute_refuses_empty_id(mock_client, mock_guard):
    """Approved but snapshot_id empty (never bound at approval) → snapshot_failed
    and NO create — execute never re-resolves a default the DBA didn't approve."""
    rds = _rds_instance(status="available")
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = create_rds_snapshot_impl(
        MagicMock(), cluster_id="rds-mysql-1", approved=True, approval_id="aid-1")
    assert out["status"] == "snapshot_failed"
    rds.create_db_snapshot.assert_not_called()


@patch(f"{_SN}.verify_approval")
@patch(f"{_SN}.client_for_cluster")
def test_snapshot_execute_uses_bound_id(mock_client, mock_guard):
    rds = _rds_instance(status="available")
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = create_rds_snapshot_impl(
        MagicMock(), cluster_id="rds-mysql-1", snapshot_id="dbops-snap-1",
        approved=True, approval_id="aid-1")
    assert out["status"] == "snapshot_creating"
    assert out["snapshot_id"] == "dbops-snap-1"
    rds.create_db_snapshot.assert_called_once_with(
        DBInstanceIdentifier="rds-mysql-1", DBSnapshotIdentifier="dbops-snap-1")


@patch(f"{_SN}.verify_approval")
@patch(f"{_SN}.client_for_cluster")
def test_snapshot_toctou_not_available_refuses(mock_client, mock_guard):
    rds = _rds_instance(status="stopped")
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = create_rds_snapshot_impl(
        MagicMock(), cluster_id="rds-mysql-1", snapshot_id="dbops-snap-1",
        approved=True, approval_id="aid-1")
    assert out["status"] == "not_applicable"
    rds.create_db_snapshot.assert_not_called()


@patch(f"{_SN}.verify_approval")
@patch(f"{_SN}.client_for_cluster")
def test_snapshot_failure_no_leak(mock_client, mock_guard):
    rds = _rds_instance(status="available")
    rds.create_db_snapshot.side_effect = Exception("SECRET_INTERNAL_LEAK")
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = create_rds_snapshot_impl(
        MagicMock(), cluster_id="rds-mysql-1", snapshot_id="dbops-snap-1",
        approved=True, approval_id="aid-1")
    assert out["status"] == "snapshot_failed"
    assert "SECRET_INTERNAL_LEAK" not in json.dumps(out)


@patch(f"{_SN}.client_for_cluster")
def test_snapshot_payload_hash_mismatch_rejected(mock_client):
    """A real approval minted for snapshot id X cannot create snapshot id Y."""
    rds = _rds_instance(status="available")
    mock_client.return_value = rds
    row = _approval_row(
        "create_rds_snapshot", {"cluster_id": "rds-mysql-1", "snapshot_id": "dbops-snap-1"})
    table, resource = _guarded_table(row)
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}, clear=False), \
         patch("mcp_servers.shared.approval_guard.boto3.resource", return_value=resource):
        out = create_rds_snapshot_impl(
            MagicMock(), cluster_id="rds-mysql-1", snapshot_id="dbops-snap-2",
            approved=True, approval_id="aid-1")
    assert out["status"] == "approval_denied"
    rds.create_db_snapshot.assert_not_called()
    table.update_item.assert_not_called()


# ─────────────────────── modify_rds_instance_class ───────────────────────

def test_modify_class_requires_target():
    out = modify_rds_instance_class_impl(MagicMock(), cluster_id="rds-mysql-1")
    assert out["status"] == "invalid_request"
    assert "target_class" in out["reason"]


@patch(f"{_MC}.client_for_cluster")
def test_modify_class_preview_binds_classes(mock_client):
    """PREVIEW → approval_required binding cluster_id + target + the current
    class read NOW (TOCTOU baseline); nothing modified."""
    rds = _rds_instance(status="available", instance_class="db.t3.medium")
    mock_client.return_value = rds
    out = modify_rds_instance_class_impl(
        MagicMock(), cluster_id="rds-mysql-1", target_class="db.r6g.large")
    assert out["status"] == "approval_required"
    assert out["target_class"] == "db.r6g.large"
    assert out["current_class"] == "db.t3.medium"
    rds.modify_db_instance.assert_not_called()


@patch(f"{_MC}.client_for_cluster")
def test_modify_class_noop_target_equals_current(mock_client):
    rds = _rds_instance(status="available", instance_class="db.r6g.large")
    mock_client.return_value = rds
    out = modify_rds_instance_class_impl(
        MagicMock(), cluster_id="rds-mysql-1", target_class="db.r6g.large")
    assert out["status"] == "not_applicable"
    rds.modify_db_instance.assert_not_called()


@patch(f"{_MC}.client_for_cluster")
def test_modify_class_not_available_not_applicable(mock_client):
    rds = _rds_instance(status="stopped", instance_class="db.t3.medium")
    mock_client.return_value = rds
    out = modify_rds_instance_class_impl(
        MagicMock(), cluster_id="rds-mysql-1", target_class="db.r6g.large")
    assert out["status"] == "not_applicable"
    rds.modify_db_instance.assert_not_called()


@patch(f"{_MC}.verify_approval")
@patch(f"{_MC}.client_for_cluster")
def test_modify_class_execute_when_approved(mock_client, mock_guard):
    rds = _rds_instance(status="available", instance_class="db.t3.medium")
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = modify_rds_instance_class_impl(
        MagicMock(), cluster_id="rds-mysql-1", target_class="db.r6g.large",
        current_class="db.t3.medium", approved=True, approval_id="aid-1")
    assert out["status"] == "modifying"
    assert out["target_class"] == "db.r6g.large"
    rds.modify_db_instance.assert_called_once_with(
        DBInstanceIdentifier="rds-mysql-1", DBInstanceClass="db.r6g.large",
        ApplyImmediately=True)


@patch(f"{_MC}.verify_approval")
@patch(f"{_MC}.client_for_cluster")
def test_modify_class_toctou_current_changed_refuses(mock_client, mock_guard):
    """The current class drifted since approval (someone else resized) → refuse,
    no modify. The approval was bound to current='db.t3.medium'."""
    rds = _rds_instance(status="available", instance_class="db.m5.large")
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = modify_rds_instance_class_impl(
        MagicMock(), cluster_id="rds-mysql-1", target_class="db.r6g.large",
        current_class="db.t3.medium", approved=True, approval_id="aid-1")
    assert out["status"] == "state_changed"
    rds.modify_db_instance.assert_not_called()


@patch(f"{_MC}.verify_approval")
@patch(f"{_MC}.client_for_cluster")
def test_modify_class_failure_no_leak(mock_client, mock_guard):
    rds = _rds_instance(status="available", instance_class="db.t3.medium")
    rds.modify_db_instance.side_effect = Exception("SECRET_INTERNAL_LEAK")
    mock_client.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = modify_rds_instance_class_impl(
        MagicMock(), cluster_id="rds-mysql-1", target_class="db.r6g.large",
        current_class="db.t3.medium", approved=True, approval_id="aid-1")
    assert out["status"] == "modify_failed"
    assert "SECRET_INTERNAL_LEAK" not in json.dumps(out)


@patch(f"{_MC}.client_for_cluster")
def test_modify_class_payload_hash_mismatch_rejected(mock_client):
    """A real approval minted for target X cannot drive a resize to target Y."""
    rds = _rds_instance(status="available", instance_class="db.t3.medium")
    mock_client.return_value = rds
    row = _approval_row("modify_rds_instance_class", {
        "cluster_id": "rds-mysql-1", "target_class": "db.r6g.large",
        "current_class": "db.t3.medium"})
    table, resource = _guarded_table(row)
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}, clear=False), \
         patch("mcp_servers.shared.approval_guard.boto3.resource", return_value=resource):
        out = modify_rds_instance_class_impl(
            MagicMock(), cluster_id="rds-mysql-1", target_class="db.r6g.xlarge",
            current_class="db.t3.medium", approved=True, approval_id="aid-1")
    assert out["status"] == "approval_denied"
    rds.modify_db_instance.assert_not_called()
    table.update_item.assert_not_called()


# ───────────────────── allowlist + _project round-trip ─────────────────────

def test_new_action_types_in_request_approval_allowlist():
    """request_approval must accept the 3 new action_types or the approval loop
    dead-ends. Exercised via the real impl with a mocked DDB table."""
    from mcp_servers.operations.tools import request_approval as ra
    for action_type, details in [
        ("reboot_rds_instance", {"cluster_id": "rds-mysql-1"}),
        ("create_rds_snapshot", {"cluster_id": "rds-mysql-1", "snapshot_id": "dbops-snap-1"}),
        ("modify_rds_instance_class",
         {"cluster_id": "rds-mysql-1", "target_class": "db.r6g.large", "current_class": "db.t3.medium"}),
    ]:
        with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}), \
             patch.object(ra, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            out = ra.request_approval_impl(
                None, cluster_id="rds-mysql-1", action_type=action_type,
                action_details=details)
        assert out["status"] == "pending", action_type
        assert out["action_type"] == action_type


def test_project_binds_new_action_type_fields():
    """_project must bind EXACTLY the operation-defining fields per new action —
    a different value must change the hash (payload binding)."""
    # reboot: cluster_id only
    assert canonical_action_hash("reboot_rds_instance", {"cluster_id": "a"}) != \
        canonical_action_hash("reboot_rds_instance", {"cluster_id": "b"})
    # snapshot: cluster_id + snapshot_id
    base = canonical_action_hash(
        "create_rds_snapshot", {"cluster_id": "a", "snapshot_id": "s1"})
    assert base != canonical_action_hash(
        "create_rds_snapshot", {"cluster_id": "a", "snapshot_id": "s2"})
    assert base != canonical_action_hash(
        "create_rds_snapshot", {"cluster_id": "b", "snapshot_id": "s1"})
    # modify class: cluster_id + target_class + current_class all bound
    mc = canonical_action_hash("modify_rds_instance_class", {
        "cluster_id": "a", "target_class": "t1", "current_class": "c1"})
    assert mc != canonical_action_hash("modify_rds_instance_class", {
        "cluster_id": "a", "target_class": "t2", "current_class": "c1"})
    assert mc != canonical_action_hash("modify_rds_instance_class", {
        "cluster_id": "a", "target_class": "t1", "current_class": "c2"})


# ===========================================================================
# COULD-NOT-ASK vs DOES-NOT-EXIST
# ===========================================================================
# All three tools returned None from their own _describe helper both when the
# describe RAISED and when RDS said there was no such instance, and both landed on
# not_applicable with "인스턴스를 조회할 수 없습니다, 대상 식별자를 확인하세요". After a
# throttle or an AccessDenied that sends the DBA to fix a name that was never
# wrong, and the identifier is the one thing that is not the problem. The split
# modify_rds_instance_params already shipped is applied here.
#
# Driven with verify_approval SPIED, so "consumed" means the single-use approval
# is gone. `describes_when_approved` differs per tool: reboot and snapshot
# pre-check before the `if not approved` branch, modify-class pre-checks inside it.

_AVAIL = {"DBInstanceStatus": "available", "DBInstanceClass": "db.t4g.micro"}

_TOOLS = (
    ("reboot", _RB, reboot_rds_instance_impl, {}, 2, "reboot_db_instance"),
    ("modify_class", _MC, modify_rds_instance_class_impl,
     {"target_class": "db.t4g.small", "current_class": "db.t4g.micro"}, 1,
     "modify_db_instance"),
    ("snapshot", _SN, create_rds_snapshot_impl, {"snapshot_id": "s1"}, 2,
     "create_db_snapshot"),
)


def _drive(mod_path, impl, kwargs, describe_side, approved):
    """(response, approvals_consumed, rds_mock)."""
    rds = MagicMock()
    rds.describe_db_instances.side_effect = describe_side
    consumed = []
    with patch(f"{mod_path}.client_for_cluster", return_value=rds), \
         patch(f"{mod_path}.verify_approval",
               side_effect=lambda *a, **k: consumed.append(1) or {"ok": True}):
        resp = impl(MagicMock(), "inst-1", approved=approved, approval_id="a1", **kwargs)
    return resp, len(consumed), rds


def test_a_failed_describe_does_not_blame_the_target_identifier():
    for name, mod_path, impl, kwargs, _n, _write in _TOOLS:
        resp, consumed, _ = _drive(mod_path, impl, kwargs, Exception("Throttling"), False)
        assert resp["status"] == "lookup_failed", (name, resp)
        assert consumed == 0, (name, "a pre-approval failure must not consume anything")
        reason = resp["reason"]
        assert "throttling" in reason.lower(), (name, reason)
        assert "문제가 아니므로" in reason, (name, "must say the identifier is NOT the problem")
        # And no exception text reaches the payload.
        assert "Throttling(" not in reason and "Traceback" not in reason, (name, reason)


def test_a_missing_instance_does_blame_the_target_identifier():
    for name, mod_path, impl, kwargs, _n, _write in _TOOLS:
        resp, consumed, _ = _drive(mod_path, impl, kwargs, [{"DBInstances": []}], False)
        assert resp["status"] == "not_applicable", (name, resp)
        assert consumed == 0, (name,)
        assert "존재하지 않습니다" in resp["reason"], (name, resp["reason"])


def test_a_failed_recheck_says_the_approval_was_consumed_and_writes_nothing():
    """This failure IS post-consume and cannot be moved earlier: the re-check
    exists to look at the state in the window after approval. So the message has
    to say the approval is gone, or the DBA re-issues the same call forever."""
    for name, mod_path, impl, kwargs, describes, write in _TOOLS:
        side = [{"DBInstances": [_AVAIL]}] * (describes - 1) + [Exception("Throttling")]
        resp, consumed, rds = _drive(mod_path, impl, kwargs, side, True)
        assert resp["status"] == "lookup_failed", (name, resp)
        assert consumed == 1, (name, "the re-check runs after the consume by design")
        assert "소진" in resp["reason"], (name, "must say the approval is spent")
        assert write not in str(rds.method_calls), (name, "must not write")


def test_an_instance_that_vanished_after_approval_writes_nothing():
    for name, mod_path, impl, kwargs, describes, write in _TOOLS:
        side = [{"DBInstances": [_AVAIL]}] * (describes - 1) + [{"DBInstances": []}]
        resp, consumed, rds = _drive(mod_path, impl, kwargs, side, True)
        assert resp["status"] == "not_applicable", (name, resp)
        assert consumed == 1, (name,)
        assert write not in str(rds.method_calls), (name, "must not write")


def test_the_happy_path_still_reaches_the_write_on_all_three():
    """A pre-check that refuses too much silently removes a capability, which is
    worse than the message it fixed."""
    for name, mod_path, impl, kwargs, _n, write in _TOOLS:
        rds = MagicMock()
        rds.describe_db_instances.return_value = {"DBInstances": [_AVAIL]}
        with patch(f"{mod_path}.client_for_cluster", return_value=rds), \
             patch(f"{mod_path}.verify_approval", return_value={"ok": True}):
            resp = impl(MagicMock(), "inst-1", approved=True, approval_id="a1", **kwargs)
        assert write in str(rds.method_calls), (name, resp)
        assert resp["status"] in ("rebooting", "modifying", "snapshot_creating"), (name, resp)
