"""Tests for plan_az_scaleout (P2-⑥) — READ-ONLY AZ scale-out planner.

Covers: the handler relational engine-gate (unsupported_engine on non-relational,
impl never runs); exclude_az valid → planned readers round-robin over the healthy
AZs with a concrete class + AZ + unique ids; exclude_az not in the cluster →
invalid_az; no healthy AZ left → no_healthy_az; count clamp to 10; unresolvable
writer class → needs_instance_class; describe failure → error with no str(e) leak;
generated ids avoid colliding with existing members.

Every describe_* MagicMock returns a real dict so a paginator/loop can't hang.
"""

import json
import os
from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.plan_az_scaleout import plan_az_scaleout_impl

_P = "mcp_servers.operations.tools.plan_az_scaleout"


def _rds(azs, members, cluster_id="prod-pg-1", engine="aurora-postgresql",
         writer_class="db.serverless"):
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [{
        "DBClusterIdentifier": cluster_id,
        "Engine": engine,
        "AvailabilityZones": azs,
        "DBClusterMembers": members,
    }]}
    writer_id = next((m["DBInstanceIdentifier"] for m in members if m.get("IsClusterWriter")), None)
    rds.describe_db_instances.return_value = {"DBInstances": (
        [{"DBInstanceIdentifier": writer_id, "DBInstanceClass": writer_class}] if writer_id else []
    )}
    return rds


_WRITER = [{"DBInstanceIdentifier": "w", "IsClusterWriter": True}]
_3AZ = ["ap-northeast-2a", "ap-northeast-2b", "ap-northeast-2c"]


# ───────────────────────── handler engine-gate ─────────────────────────

def test_gate_relational_only_fail_closed():
    """plan_az_scaleout is positive-gated on scale_instance (relational-only):
    a non-relational / unresolvable cluster gets unsupported_engine and the impl
    never runs (mirrors the write-tool gate — it's read-only but Aurora-shaped)."""
    os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:0:cluster:test")
    os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:0:secret:test")
    os.environ.setdefault("CACHE_DB_NAME", "dbops")
    import mcp_servers.operations.handler as handler

    assert handler._ENGINE_GATED_TOOLS["plan_az_scaleout"] == "scale_instance"

    class _Ctx:
        client_context = MagicMock()
        client_context.custom = {"bedrockAgentCoreToolName": "x___plan_az_scaleout"}

    spy = MagicMock()
    with patch.object(handler, "_resolve_family", lambda cid: "dynamodb"), \
         patch.dict(handler.TOOLS["plan_az_scaleout"], {"impl": spy}):
        out = json.loads(handler.lambda_handler({"cluster_id": "ddb-1"}, _Ctx())["content"][0]["text"])
    assert out["status"] == "unsupported_engine"
    spy.assert_not_called()


# ───────────────────────── planning ─────────────────────────

@patch(f"{_P}.client_for_cluster")
def test_plan_round_robin_excludes_az(mock_client):
    mock_client.return_value = _rds(_3AZ, _WRITER)
    out = plan_az_scaleout_impl(
        MagicMock(), cluster_id="prod-pg-1", exclude_az="ap-northeast-2b", count=4,
    )
    assert out["status"] == "planned"
    assert out["exclude_az"] == "ap-northeast-2b"
    assert out["available_azs"] == _3AZ
    assert out["healthy_azs"] == ["ap-northeast-2a", "ap-northeast-2c"]
    readers = out["planned_readers"]
    assert len(readers) == 4
    # round-robin over the two healthy AZs (2b is never used)
    assert [r["availability_zone"] for r in readers] == [
        "ap-northeast-2a", "ap-northeast-2c", "ap-northeast-2a", "ap-northeast-2c",
    ]
    # every reader carries the concrete, resolved writer class
    assert all(r["instance_class"] == "db.serverless" for r in readers)
    assert out["instance_class"] == "db.serverless"
    # unique, valid ids
    ids = [r["new_instance_id"] for r in readers]
    assert len(set(ids)) == 4
    assert "ap-northeast-2b" not in [r["availability_zone"] for r in readers]


@patch(f"{_P}.client_for_cluster")
def test_plan_no_exclude_spreads_all(mock_client):
    mock_client.return_value = _rds(_3AZ, _WRITER)
    out = plan_az_scaleout_impl(MagicMock(), cluster_id="prod-pg-1", count=3)
    assert out["status"] == "planned"
    assert out["exclude_az"] == ""
    assert out["healthy_azs"] == _3AZ
    assert [r["availability_zone"] for r in out["planned_readers"]] == _3AZ


@patch(f"{_P}.client_for_cluster")
def test_plan_explicit_class_skips_lookup(mock_client):
    rds = _rds(_3AZ, _WRITER)
    mock_client.return_value = rds
    out = plan_az_scaleout_impl(
        MagicMock(), cluster_id="prod-pg-1", count=1, instance_class="db.r6g.large",
    )
    assert out["status"] == "planned"
    assert out["planned_readers"][0]["instance_class"] == "db.r6g.large"
    rds.describe_db_instances.assert_not_called()  # no writer-class lookup


@patch(f"{_P}.client_for_cluster")
def test_plan_invalid_az(mock_client):
    mock_client.return_value = _rds(_3AZ, _WRITER)
    out = plan_az_scaleout_impl(
        MagicMock(), cluster_id="prod-pg-1", exclude_az="us-east-1z", count=1,
    )
    assert out["status"] == "invalid_az"
    assert out["available_azs"] == _3AZ


@patch(f"{_P}.client_for_cluster")
def test_plan_no_healthy_az(mock_client):
    mock_client.return_value = _rds(["ap-northeast-2a"], _WRITER)
    out = plan_az_scaleout_impl(
        MagicMock(), cluster_id="prod-pg-1", exclude_az="ap-northeast-2a", count=1,
    )
    assert out["status"] == "no_healthy_az"


@patch(f"{_P}.client_for_cluster")
def test_plan_count_clamped(mock_client):
    mock_client.return_value = _rds(_3AZ, _WRITER)
    out = plan_az_scaleout_impl(MagicMock(), cluster_id="prod-pg-1", count=50)
    assert out["status"] == "planned"
    assert len(out["planned_readers"]) == 10
    assert out.get("clamped") is True


@patch(f"{_P}.client_for_cluster")
def test_plan_needs_instance_class(mock_client):
    # writer present but describe_db_instances yields nothing → unresolvable class
    rds = _rds(_3AZ, _WRITER)
    rds.describe_db_instances.return_value = {"DBInstances": []}
    mock_client.return_value = rds
    out = plan_az_scaleout_impl(MagicMock(), cluster_id="prod-pg-1", count=1)
    assert out["status"] == "needs_instance_class"
    assert out["available_azs"] == _3AZ


@patch(f"{_P}.client_for_cluster")
def test_plan_ids_avoid_existing_members(mock_client):
    # An existing member already owns the first id the planner would mint → bump.
    members = _WRITER + [{"DBInstanceIdentifier": "prod-pg-1-az1", "IsClusterWriter": False}]
    mock_client.return_value = _rds(_3AZ, members)
    out = plan_az_scaleout_impl(MagicMock(), cluster_id="prod-pg-1", count=2)
    ids = [r["new_instance_id"] for r in out["planned_readers"]]
    assert "prod-pg-1-az1" not in ids  # collides with existing member → skipped
    assert len(set(ids)) == 2


@patch(f"{_P}.client_for_cluster")
def test_plan_describe_failure_no_leak(mock_client):
    rds = MagicMock()
    rds.describe_db_clusters.side_effect = Exception("SECRET_INTERNAL_LEAK")
    mock_client.return_value = rds
    out = plan_az_scaleout_impl(MagicMock(), cluster_id="prod-pg-1", count=1)
    assert out["status"] == "error"
    assert "SECRET_INTERNAL_LEAK" not in json.dumps(out)
