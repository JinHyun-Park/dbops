"""Tests for scale_out_with_warmup (N-④ Phase 1): approval-gated reader
scale-out that pre-queues a prewarm approval.

Covers: preview vs execute; verify_approval FAIL-CLOSED; execute creates the
reader (mocked rds) AND writes an `awaiting_instance` prewarm approval row whose
payload_hash matches what prewarm_reader.verify_approval would accept (asserted
by projecting the same dict with the same canonical_action_hash); scale-out
markers present; top_n clamped so the stored hash can't drift from prewarm's cap.
"""

import os
from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.prewarm_reader import _TOP_N_CAP
from mcp_servers.operations.tools.scale_out_with_warmup import scale_out_with_warmup_impl
from mcp_servers.shared.approval_guard import canonical_action_hash

_SO = "mcp_servers.operations.tools.scale_out_with_warmup"


def _rds():
    """rds client with a Serverless-v2 writer so the class default resolves to
    db.serverless, and a resolvable cluster."""
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [{
        "DBClusterIdentifier": "prod-pg-1",
        "Engine": "aurora-postgresql",
        "DBClusterMembers": [{"DBInstanceIdentifier": "writer-1", "IsClusterWriter": True}],
    }]}
    rds.describe_db_instances.return_value = {"DBInstances": [
        {"DBInstanceIdentifier": "writer-1", "DBInstanceClass": "db.serverless"},
    ]}
    return rds


def _approvals():
    """(boto3.resource stand-in, the approvals Table mock)."""
    table = MagicMock()
    resource = MagicMock()
    resource.Table.return_value = table
    return resource, table


def _registry(region="ap-northeast-2", role="arn:aws:iam::222:role/dbops-spoke"):
    return {"region": region, "spoke_role_arn": role}


# ───────────────────────── preview stage ─────────────────────────

def test_preview_explicit_class_writes_nothing():
    """Explicit class → preview needs no rds resolution and writes no approval row."""
    with patch(f"{_SO}.client_for_cluster") as cfc, \
         patch(f"{_SO}.boto3") as boto:
        out = scale_out_with_warmup_impl(
            MagicMock(), cluster_id="prod-pg-1", new_instance_id="reader-2",
            instance_class="db.serverless", endpoint_identifier="ep-ro", top_n=20,
        )
    assert out["status"] == "approval_required"
    assert out["instance_class"] == "db.serverless"
    assert out["top_n"] == 20
    assert "예열" in out["cli_preview"]
    cfc.assert_not_called()          # explicit class → no rds resolution
    boto.resource.assert_not_called()  # no approval row written


def test_preview_resolves_writer_class():
    """Empty class → preview resolves the WRITER's concrete class (db.serverless)
    so the DBA approves the actual billable class; no approval row written."""
    rds = _rds()
    with patch(f"{_SO}.client_for_cluster", return_value=rds), \
         patch(f"{_SO}.boto3") as boto:
        out = scale_out_with_warmup_impl(
            MagicMock(), cluster_id="prod-pg-1", new_instance_id="reader-2",
            endpoint_identifier="ep-ro", top_n=20,
        )
    assert out["status"] == "approval_required"
    assert out["instance_class"] == "db.serverless"  # concrete, resolved in preview
    assert "db.serverless" in out["cli_preview"]
    rds.create_db_instance.assert_not_called()
    boto.resource.assert_not_called()


def test_preview_unresolvable_class_asks():
    """Empty class the writer lookup can't resolve → needs_instance_class, not an
    approval_required carrying an empty class."""
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [{
        "DBClusterIdentifier": "prod-pg-1", "Engine": "aurora-postgresql",
        "DBClusterMembers": [{"DBInstanceIdentifier": "writer-1", "IsClusterWriter": True}],
    }]}
    rds.describe_db_instances.return_value = {"DBInstances": []}  # unresolvable
    with patch(f"{_SO}.client_for_cluster", return_value=rds):
        out = scale_out_with_warmup_impl(
            MagicMock(), cluster_id="prod-pg-1", new_instance_id="reader-2",
        )
    assert out["status"] == "needs_instance_class"
    rds.create_db_instance.assert_not_called()


def test_missing_instance_id_is_invalid():
    out = scale_out_with_warmup_impl(MagicMock(), cluster_id="prod-pg-1")
    assert out["status"] == "invalid_instance"


# ───────────────────────── approval FAIL-CLOSED ─────────────────────────

def test_denied_approval_never_creates_or_queues():
    rds = _rds()
    resource, table = _approvals()
    with patch(f"{_SO}.verify_approval", return_value={"ok": False, "reason": "nope"}), \
         patch(f"{_SO}.client_for_cluster", return_value=rds), \
         patch(f"{_SO}.boto3.resource", return_value=resource):
        out = scale_out_with_warmup_impl(
            MagicMock(), cluster_id="prod-pg-1", new_instance_id="reader-2",
            approved=True, approval_id="aid-1",
        )
    assert out["status"] == "approval_denied"
    rds.create_db_instance.assert_not_called()
    table.put_item.assert_not_called()


# ───────────────────────── execute stage ─────────────────────────

@patch(f"{_SO}.verify_approval", return_value={"ok": True})
def test_execute_requires_bound_class(_guard):
    """Approved but instance_class empty (never bound at approval) → add_failed,
    NO create, NO prewarm queue — execute never resolves a class post-approval."""
    rds = _rds()
    resource, table = _approvals()
    with patch(f"{_SO}.client_for_cluster", return_value=rds), \
         patch(f"{_SO}.lookup_cluster", return_value=_registry()), \
         patch(f"{_SO}.boto3.resource", return_value=resource), \
         patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}, clear=False):
        out = scale_out_with_warmup_impl(
            MagicMock(), cluster_id="prod-pg-1", new_instance_id="reader-2",
            approved=True, approval_id="aid-1",
        )
    assert out["status"] == "add_failed"
    rds.create_db_instance.assert_not_called()
    table.put_item.assert_not_called()


@patch(f"{_SO}.verify_approval", return_value={"ok": True})
def test_execute_creates_reader_and_queues_awaiting_prewarm(_guard):
    rds = _rds()
    resource, table = _approvals()
    with patch(f"{_SO}.client_for_cluster", return_value=rds), \
         patch(f"{_SO}.lookup_cluster", return_value=_registry()), \
         patch(f"{_SO}.boto3.resource", return_value=resource), \
         patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}, clear=False):
        out = scale_out_with_warmup_impl(
            MagicMock(), cluster_id="prod-pg-1", new_instance_id="reader-2",
            instance_class="db.serverless", endpoint_identifier="ep-ro", top_n=20,
            approved=True, approval_id="aid-1",
        )

    assert out["status"] == "scaleout_started"
    assert out["instance_id"] == "reader-2"
    assert out["warm_approval_id"]

    # a. reader created with the approved class (db.serverless), scale-out tag.
    call = rds.create_db_instance.call_args.kwargs
    assert call["DBInstanceIdentifier"] == "reader-2"
    assert call["DBClusterIdentifier"] == "prod-pg-1"
    assert call["Engine"] == "aurora-postgresql"
    assert call["DBInstanceClass"] == "db.serverless"
    assert {"Key": "dbops:managed", "Value": "scale-out"} in call["Tags"]

    # b. an awaiting_instance prewarm approval row with the scale-out markers.
    item = table.put_item.call_args.kwargs["Item"]
    assert item["approval_status"] == "awaiting_instance"
    assert item["action_type"] == "prewarm_reader"
    assert item["scaleout"] is True
    assert item["reader_instance_id"] == "reader-2"
    assert item["region"] == "ap-northeast-2"
    assert item["spoke_role_arn"] == "arn:aws:iam::222:role/dbops-spoke"
    assert item["approval_id"] == out["warm_approval_id"]

    # payload_hash is EXACTLY what prewarm_reader.verify_approval will project +
    # accept: same canonical_action_hash over {cluster_id, reader_instance_id,
    # endpoint_identifier, top_n}. Equality here = no drift.
    expected = canonical_action_hash("prewarm_reader", {
        "cluster_id": "prod-pg-1", "reader_instance_id": "reader-2",
        "endpoint_identifier": "ep-ro", "top_n": 20,
    })
    assert item["payload_hash"] == expected


@patch(f"{_SO}.verify_approval", return_value={"ok": True})
def test_top_n_clamped_into_hash(_guard):
    """top_n over prewarm's cap is clamped BEFORE hashing, so the stored hash
    equals what prewarm (which re-clamps to the same cap) will verify."""
    rds = _rds()
    resource, table = _approvals()
    with patch(f"{_SO}.client_for_cluster", return_value=rds), \
         patch(f"{_SO}.lookup_cluster", return_value=_registry()), \
         patch(f"{_SO}.boto3.resource", return_value=resource), \
         patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}, clear=False):
        scale_out_with_warmup_impl(
            MagicMock(), cluster_id="prod-pg-1", new_instance_id="reader-2",
            instance_class="db.serverless", top_n=999,
            approved=True, approval_id="aid-1",
        )
    item = table.put_item.call_args.kwargs["Item"]
    assert item["action_details"]["top_n"] == _TOP_N_CAP
    expected = canonical_action_hash("prewarm_reader", {
        "cluster_id": "prod-pg-1", "reader_instance_id": "reader-2",
        "endpoint_identifier": "", "top_n": _TOP_N_CAP,
    })
    assert item["payload_hash"] == expected
