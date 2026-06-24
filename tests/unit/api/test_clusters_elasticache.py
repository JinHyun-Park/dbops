"""ElastiCache registration via api/clusters handler."""
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_CLUSTERS_DIR = Path(__file__).resolve().parents[3] / "api" / "clusters"
# Push clusters/ dir so `import seeder` and `import engine_family` both resolve.
sys.path.insert(0, str(_CLUSTERS_DIR))

_H = _CLUSTERS_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("clusters_handler_ec", _H)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

import pytest


@pytest.fixture(autouse=True)
def _clusters_table_env(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")


def _table():
    t = MagicMock()
    t.put_item.return_value = {}
    return t


def _body(name="my-redis", engine="redis"):
    return {"account_id": "111122223333", "region": "ap-northeast-2",
            "resource_name": name, "engine": engine}


def test_register_redis_replication_group():
    fake = MagicMock()
    fake.describe_replication_groups.return_value = {
        "ReplicationGroups": [{
            "ReplicationGroupId": "my-redis", "Status": "available",
            "ClusterEnabled": True, "AuthTokenEnabled": True,
            "TransitEncryptionEnabled": True,
            "NodeGroups": [{"NodeGroupId": "0001"}, {"NodeGroupId": "0002"}],
            "MemberClusters": ["my-redis-0001-001", "my-redis-0001-002"],
            "CacheNodeType": "cache.r7g.large",
        }]
    }
    table = _table()
    with patch.object(handler, "_elasticache_client_for", return_value=fake):
        r = handler._register_elasticache(table, _body())
    assert r["statusCode"] in (201, 207)
    item = table.put_item.call_args.kwargs["Item"]
    assert item["engine_family"] == "elasticache"
    assert item["resource_type"] == "elasticache-redis"
    assert item["cluster_id"] == "my-redis"   # real name, no slug
    rd = item["resource_details"]
    assert rd["cluster_mode"] is True and rd["num_node_groups"] == 2


def test_register_memcached_cache_cluster_fallback():
    fake = MagicMock()
    # not a replication group → fall back to cache cluster
    fake.describe_replication_groups.side_effect = Exception("ReplicationGroupNotFoundFault")
    fake.describe_cache_clusters.return_value = {
        "CacheClusters": [{
            "CacheClusterId": "my-memcached", "Engine": "memcached",
            "EngineVersion": "1.6.22", "CacheClusterStatus": "available",
            "CacheNodeType": "cache.t4g.small", "NumCacheNodes": 3,
        }]
    }
    table = _table()
    with patch.object(handler, "_elasticache_client_for", return_value=fake):
        r = handler._register_elasticache(table, _body(name="my-memcached", engine="memcached"))
    assert r["statusCode"] in (201, 207)
    item = table.put_item.call_args.kwargs["Item"]
    assert item["resource_type"] == "elasticache-memcached"
    assert item["resource_details"]["num_cache_nodes"] == 3


def test_register_not_found_warns():
    fake = MagicMock()
    fake.describe_replication_groups.side_effect = Exception("not found")
    fake.describe_cache_clusters.side_effect = Exception("CacheClusterNotFound")
    table = _table()
    with patch.object(handler, "_elasticache_client_for", return_value=fake):
        r = handler._register_elasticache(table, _body())
    assert r["statusCode"] == 207  # registered_with_warning
    assert table.put_item.call_args.kwargs["Item"]["connection_status"] == "failed"


def test_handle_register_dispatches_elasticache():
    fake = MagicMock()
    fake.describe_replication_groups.return_value = {"ReplicationGroups": [
        {"ReplicationGroupId": "r", "Status": "available", "ClusterEnabled": False,
         "MemberClusters": ["r-001"], "CacheNodeType": "cache.t4g.micro"}]}
    table = _table()
    with patch.object(handler, "_elasticache_client_for", return_value=fake):
        r = handler._handle_register(table, _body(name="r", engine="redis"))
    assert r["statusCode"] in (201, 207)
    assert table.put_item.call_args.kwargs["Item"]["engine_family"] == "elasticache"


def test_register_missing_fields_400():
    r = handler._register_elasticache(_table(), {"engine": "redis"})
    assert r["statusCode"] == 400
