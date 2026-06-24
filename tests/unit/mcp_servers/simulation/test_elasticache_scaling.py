import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_T = Path(__file__).resolve().parents[4] / "mcp-servers/mcp_servers/simulation/tools/elasticache_scaling_simulation.py"
_spec = importlib.util.spec_from_file_location("ec_scale", _T)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _wire():
    mod.lookup_cluster = lambda cid: {"resource_name": "my-redis", "region": "ap-northeast-2",
                                      "spoke_role_arn": "", "resource_details": {"engine": "redis"}}
    ec = MagicMock()
    ec.describe_replication_groups.return_value = {"ReplicationGroups": [
        {"ReplicationGroupId": "my-redis", "CacheNodeType": "cache.t4g.micro",
         "MemberClusters": ["my-redis-001"], "NodeGroups": [{"NodeGroupId": "0001",
            "NodeGroupMembers": [{"CacheClusterId": "my-redis-001"}]}]}]}
    mod.client_for_cluster = lambda cid, svc: ec
    mod.compute_node_resize_cost = lambda **k: {"status": "ok", "current_monthly": 14.6,
        "proposed_monthly": 219.0, "delta_monthly": 204.4, "note": "n"}
    return ec


def test_resize_returns_cost():
    _wire()
    r = mod.simulate_elasticache_node_resize_impl(None, cluster_id="my-redis",
        new_node_type="cache.r7g.large", new_node_count=1)
    assert r["status"] == "ok"
    assert r["proposed_monthly"] == 219.0


def test_resize_missing_cluster_id():
    assert mod.simulate_elasticache_node_resize_impl(None)["status"] in ("error", "invalid")
