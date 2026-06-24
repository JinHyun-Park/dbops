"""ElastiCache write tools — approval-gated, FAIL-CLOSED."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_BASE = Path(__file__).resolve().parents[4] / "mcp-servers/mcp_servers/operations/tools"


def _load(name):
    p = _BASE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

nodetype = _load("modify_elasticache_node_type")
snapshot = _load("create_elasticache_snapshot")


def _wire(mod, engine="redis", node_type="cache.t4g.micro", guard_ok=True):
    """Patch lookup_cluster + client_for_cluster + verify_approval on the module."""
    mod.lookup_cluster = lambda cid: {
        "resource_name": "my-redis", "region": "ap-northeast-2", "spoke_role_arn": "",
        "resource_details": {"engine": engine},
    }
    ec = MagicMock()
    ec.describe_replication_groups.return_value = {"ReplicationGroups": [
        {"ReplicationGroupId": "my-redis", "CacheNodeType": node_type,
         "MemberClusters": ["my-redis-001"],
         "NodeGroups": [{"NodeGroupId": "0001", "NodeGroupMembers": [{"CacheClusterId": "my-redis-001"}]}]}]}
    mod.client_for_cluster = lambda cid, svc: ec
    mod.verify_approval = lambda *a, **k: {"ok": True} if guard_ok else {"ok": False, "reason": "denied"}
    return ec


def test_node_type_requires_approval():
    ec = _wire(nodetype)
    r = nodetype.modify_elasticache_node_type_impl(None, cluster_id="my-redis", node_type="cache.r7g.large")
    assert r["status"] == "approval_required"
    ec.modify_replication_group.assert_not_called()


def test_node_type_executes_when_approved():
    ec = _wire(nodetype)
    r = nodetype.modify_elasticache_node_type_impl(
        None, cluster_id="my-redis", node_type="cache.r7g.large", approved=True, approval_id="a1")
    assert r["status"] == "ok"
    ec.modify_replication_group.assert_called_once()
    kw = ec.modify_replication_group.call_args.kwargs
    assert kw["CacheNodeType"] == "cache.r7g.large" and kw["ApplyImmediately"] is True


def test_node_type_guard_denied_no_write():
    ec = _wire(nodetype, guard_ok=False)
    r = nodetype.modify_elasticache_node_type_impl(
        None, cluster_id="my-redis", node_type="cache.r7g.large", approved=True, approval_id="a1")
    assert r["status"] == "approval_denied"
    ec.modify_replication_group.assert_not_called()


def test_node_type_equal_rejected():
    ec = _wire(nodetype, node_type="cache.r7g.large")
    r = nodetype.modify_elasticache_node_type_impl(None, cluster_id="my-redis", node_type="cache.r7g.large")
    assert r["status"] in ("invalid", "error")
    ec.modify_replication_group.assert_not_called()


def test_snapshot_requires_approval():
    ec = _wire(snapshot)
    r = snapshot.create_elasticache_snapshot_impl(None, cluster_id="my-redis", snapshot_name="snap1")
    assert r["status"] == "approval_required"
    ec.create_snapshot.assert_not_called()


def test_snapshot_executes_when_approved():
    ec = _wire(snapshot)
    r = snapshot.create_elasticache_snapshot_impl(
        None, cluster_id="my-redis", snapshot_name="snap1", approved=True, approval_id="a1")
    assert r["status"] == "ok"
    ec.create_snapshot.assert_called_once()
    assert ec.create_snapshot.call_args.kwargs["SnapshotName"] == "snap1"


def test_snapshot_guard_denied_no_write():
    ec = _wire(snapshot, guard_ok=False)
    r = snapshot.create_elasticache_snapshot_impl(
        None, cluster_id="my-redis", snapshot_name="snap1", approved=True, approval_id="a1")
    assert r["status"] == "approval_denied"
    ec.create_snapshot.assert_not_called()


def test_snapshot_memcached_unsupported():
    ec = _wire(snapshot, engine="memcached")
    r = snapshot.create_elasticache_snapshot_impl(None, cluster_id="mc", snapshot_name="s")
    assert r["status"] == "unsupported_engine"
    ec.create_snapshot.assert_not_called()
