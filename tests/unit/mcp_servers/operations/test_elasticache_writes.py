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


# ===== no raw exception text in any response field =====
#
# The describes below run BEFORE any approval exists, so a plain chat user can
# reach these failure messages. An AWS error there carries the hub account id,
# the platform role name and the target ARN, so the response must stay static and
# the detail must go to CloudWatch via the module logger.

_LEAK_MSG = (
    "User: arn:aws:sts::123456789012:assumed-role/dbops-dev-operations-role/boom "
    "is not authorized to perform elasticache:ModifyReplicationGroup on "
    "arn:aws:elasticache:ap-northeast-2:123456789012:replicationgroup:my-redis"
)


def _client_error():
    """A realistic AccessDenied ClientError: str(e) carries the account id, the
    platform role name and the target ARN, and .response carries the Code."""
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": _LEAK_MSG}},
        "ModifyReplicationGroup",
    )


def _no_exception_text(result, allow_code=""):
    """No response value may carry the raw exception MESSAGE (project hard rule).

    `allow_code` exempts the bounded AWS error CODE, which the POST-WRITE path
    keeps on purpose: the approval is already consumed there, so without the code
    the DBA cannot tell AccessDenied from an invalid replication-group state
    without minting and burning a second approval. The pre-approval describes pass
    nothing, so the code must be absent there.
    """
    blob = " ".join(str(v) for v in result.values())
    for leak in ("123456789012", "arn:aws", "assumed-role", "AccessDeniedException",
                 "boom", "not authorized", "Traceback", "ClientError"):
        if leak == allow_code:
            continue
        assert leak not in blob, f"raw exception text leaked into response: {result}"


def test_node_type_lookup_failure_no_exception_text():
    """Pre-approval describe failure: static reason, no leak."""
    ec = _wire(nodetype)
    ec.describe_replication_groups.side_effect = _client_error()
    r = nodetype.modify_elasticache_node_type_impl(
        None, cluster_id="my-redis", node_type="cache.r7g.large")
    assert r["status"] == "error"
    _no_exception_text(r)
    assert "replication group 조회" in r["reason"]  # which step broke
    ec.modify_replication_group.assert_not_called()


def test_node_type_modify_failure_no_exception_text():
    """Post-write path: the approval is already consumed, so the bounded AWS error
    CODE stays in the reason (the DBA must not have to burn a second approval to
    learn it was AccessDenied). The exception MESSAGE still must not appear."""
    ec = _wire(nodetype)
    ec.modify_replication_group.side_effect = _client_error()
    r = nodetype.modify_elasticache_node_type_impl(
        None, cluster_id="my-redis", node_type="cache.r7g.large", approved=True, approval_id="a1")
    assert r["status"] == "error"
    _no_exception_text(r, allow_code="AccessDeniedException")
    assert "AccessDeniedException" in r["reason"]  # the actionable AWS code
    assert _LEAK_MSG not in r["reason"]  # but never the message
    assert "modify_replication_group" in r["reason"]  # which step broke


def test_snapshot_failure_no_exception_text():
    """Post-write path: AWS error CODE kept, exception MESSAGE never."""
    ec = _wire(snapshot)
    ec.create_snapshot.side_effect = _client_error()
    r = snapshot.create_elasticache_snapshot_impl(
        None, cluster_id="my-redis", snapshot_name="snap1", approved=True, approval_id="a1")
    assert r["status"] == "error"
    _no_exception_text(r, allow_code="AccessDeniedException")
    assert "AccessDeniedException" in r["reason"]
    assert _LEAK_MSG not in r["reason"]
    assert "create_snapshot" in r["reason"]


def test_reboot_lookup_failure_no_exception_text():
    """Pre-approval describe failure: static reason, no leak."""
    ec = _wire_reboot(reboot)
    ec.describe_replication_groups.side_effect = _client_error()
    r = reboot.reboot_elasticache_impl(None, cluster_id="my-redis")
    assert r["status"] == "error"
    _no_exception_text(r)
    assert "replication group 조회" in r["reason"]
    ec.reboot_cache_cluster.assert_not_called()


def test_reboot_failure_no_exception_text():
    """Post-write path: AWS error CODE kept, exception MESSAGE never."""
    ec = _wire_reboot(reboot)
    ec.reboot_cache_cluster.side_effect = _client_error()
    r = reboot.reboot_elasticache_impl(None, cluster_id="my-redis", approved=True, approval_id="a1")
    assert r["status"] == "error"
    _no_exception_text(r, allow_code="AccessDeniedException")
    assert "AccessDeniedException" in r["reason"]
    assert _LEAK_MSG not in r["reason"]
    assert "재부팅" in r["reason"]


def test_failover_lookup_failure_no_exception_text():
    """Pre-approval describe failure: static reason, no leak."""
    ec = _wire_failover(failover)
    ec.describe_replication_groups.side_effect = _client_error()
    r = failover.test_elasticache_failover_impl(None, cluster_id="my-redis")
    assert r["status"] == "error"
    _no_exception_text(r)
    assert "replication group 조회" in r["reason"]
    ec.test_failover.assert_not_called()


def test_post_write_non_clienterror_still_returns_error_not_raise():
    """A NON-ClientError on the post-write path must not crash inside the except
    block. The four tools read `e.response["Error"]["Code"]`, which only exists on
    a ClientError, so the isinstance guard is load-bearing: drop it and a plain
    Exception raises AttributeError out of the handler on a path where the
    single-use approval is ALREADY consumed, losing the error status entirely."""
    ec = _wire(nodetype)
    ec.modify_replication_group.side_effect = RuntimeError(_LEAK_MSG)
    r = nodetype.modify_elasticache_node_type_impl(
        None, cluster_id="my-redis", node_type="cache.r7g.large", approved=True, approval_id="a1")
    assert r["status"] == "error"
    _no_exception_text(r)  # no code to allow: there is no AWS code on a RuntimeError
    assert "modify_replication_group" in r["reason"]


def test_failover_failure_no_exception_text():
    """Post-write path: AWS error CODE kept, exception MESSAGE never."""
    ec = _wire_failover(failover)
    ec.test_failover.side_effect = _client_error()
    r = failover.test_elasticache_failover_impl(
        None, cluster_id="my-redis", approved=True, approval_id="a1")
    assert r["status"] == "error"
    _no_exception_text(r, allow_code="AccessDeniedException")
    assert "AccessDeniedException" in r["reason"]
    assert _LEAK_MSG not in r["reason"]
    assert "test_failover" in r["reason"]


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
