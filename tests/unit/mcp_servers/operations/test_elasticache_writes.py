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


reboot = _load("reboot_elasticache")
failover = _load("test_elasticache_failover")


def _wire_reboot(mod, members=("my-redis-001",), guard_ok=True):
    mod.lookup_cluster = lambda cid: {"resource_name": "my-redis", "resource_details": {"engine": "redis"}}
    ec = MagicMock()
    ec.describe_replication_groups.return_value = {"ReplicationGroups": [
        {"ReplicationGroupId": "my-redis",
         "NodeGroups": [{"NodeGroupId": "0001",
                         "NodeGroupMembers": [{"CacheClusterId": m} for m in members]}],
         "MemberClusters": list(members)}]}
    ec.describe_cache_clusters.return_value = {"CacheClusters": [
        {"CacheClusterId": members[0], "CacheNodes": [{"CacheNodeId": "0001"}]}]}
    mod.client_for_cluster = lambda cid, svc: ec
    mod.verify_approval = lambda *a, **k: {"ok": True} if guard_ok else {"ok": False, "reason": "denied"}
    return ec


def test_reboot_requires_approval_then_executes():
    ec = _wire_reboot(reboot)
    r1 = reboot.reboot_elasticache_impl(None, cluster_id="my-redis")
    assert r1["status"] == "approval_required"
    ec.reboot_cache_cluster.assert_not_called()
    r2 = reboot.reboot_elasticache_impl(None, cluster_id="my-redis", approved=True, approval_id="a1")
    assert r2["status"] == "ok"
    ec.reboot_cache_cluster.assert_called_once()


def test_reboot_guard_denied_no_write():
    ec = _wire_reboot(reboot, guard_ok=False)
    r = reboot.reboot_elasticache_impl(None, cluster_id="my-redis", approved=True, approval_id="a1")
    assert r["status"] == "approval_denied"
    ec.reboot_cache_cluster.assert_not_called()


def _wire_failover(mod, replicas=1, guard_ok=True):
    mod.lookup_cluster = lambda cid: {"resource_name": "my-redis", "resource_details": {"engine": "redis"}}
    ec = MagicMock()
    members = [{"CacheClusterId": "my-redis-001"}]
    if replicas:
        members.append({"CacheClusterId": "my-redis-002"})
    ec.describe_replication_groups.return_value = {"ReplicationGroups": [
        {"ReplicationGroupId": "my-redis",
         "NodeGroups": [{"NodeGroupId": "0001", "NodeGroupMembers": members}]}]}
    mod.client_for_cluster = lambda cid, svc: ec
    mod.verify_approval = lambda *a, **k: {"ok": True} if guard_ok else {"ok": False, "reason": "denied"}
    return ec


def test_failover_requires_approval_then_executes():
    ec = _wire_failover(failover, replicas=1)
    r1 = failover.test_elasticache_failover_impl(None, cluster_id="my-redis")
    assert r1["status"] == "approval_required"
    r2 = failover.test_elasticache_failover_impl(None, cluster_id="my-redis", approved=True, approval_id="a1")
    assert r2["status"] == "ok"
    ec.test_failover.assert_called_once()
    assert ec.test_failover.call_args.kwargs["NodeGroupId"] == "0001"


def test_failover_no_replica_rejected():
    ec = _wire_failover(failover, replicas=0)
    r = failover.test_elasticache_failover_impl(None, cluster_id="my-redis", approved=True, approval_id="a1")
    assert r["status"] in ("invalid", "unsupported_engine")
    ec.test_failover.assert_not_called()


# Engine-gate checks: all 4 EC-4 write tools must be in _ENGINE_GATED_TOOLS
def test_ec4_write_tools_are_engine_gated():
    import importlib.util as _iu
    import os as _os
    import sys as _sys
    # handler.py instantiates CacheClient() at import time which reads these env vars
    _os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:0:cluster:test")
    _os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:0:secret:test")
    _os.environ.setdefault("CACHE_DB_NAME", "dbops")
    _p = Path(__file__).resolve().parents[4] / "mcp-servers/mcp_servers/operations/handler.py"
    spec = _iu.spec_from_file_location("handler_ec4", _p)
    h = _iu.module_from_spec(spec)
    _sys.modules["handler_ec4"] = h
    spec.loader.exec_module(h)
    for tool in ("modify_elasticache_node_type", "create_elasticache_snapshot",
                 "reboot_elasticache", "test_elasticache_failover"):
        assert h._ENGINE_GATED_TOOLS.get(tool) == "elasticache_write", \
            f"{tool} not engine-gated with elasticache_write"
