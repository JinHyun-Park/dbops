"""ElastiCache live deep-read tool — read-only, mocked connection."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_T = Path(__file__).resolve().parents[4] / "mcp-servers/mcp_servers/operations/tools/elasticache_live_read.py"
_spec = importlib.util.spec_from_file_location("ec_live_read", _T)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class _FakeRedis:
    """Records every method call so the test can assert the read-only allowlist."""
    def __init__(self):
        self.calls = []
    def info(self, section):
        self.calls.append(("info", section))
        return {"used_memory": 1024} if section == "memory" else {"section": section}
    def slowlog_get(self, n):
        self.calls.append(("slowlog_get", n))
        return [{"id": 1, "duration": 12000, "start_time": 1700000000, "command": ["GET", "bigkey", "x" * 500]}]
    def client_list(self):
        self.calls.append(("client_list",))
        return [{"addr": "10.0.0.1:1"}, {"addr": "10.0.0.2:2"}]
    def memory_stats(self):
        self.calls.append(("memory_stats",))
        return {"total.allocated": 2048, "keys.count": 10, "peak.allocated": 4096, "dataset.bytes": 900}


def _patch(monkeypatch_targets, redis_client=None, mc_stats=None, row=None, token="tok"):
    mod.lookup_cluster = lambda cid: row or {
        "region": "ap-northeast-2", "spoke_role_arn": "", "resource_name": "my-redis",
        "engine": "redis",
        "resource_details": {"engine": "redis", "tls_enabled": True, "auth_enabled": True,
                             "auth_secret_arn": "arn:secret"},
    }
    sess = MagicMock()
    ec = MagicMock()
    ec.describe_replication_groups.return_value = {"ReplicationGroups": [
        {"ReplicationGroupId": "my-redis",
         "NodeGroups": [{"PrimaryEndpoint": {"Address": "my-redis.cache.amazonaws.com", "Port": 6379}}]}]}
    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": token}
    sess.client.side_effect = lambda svc: ec if svc == "elasticache" else sm
    mod.session_for = lambda region, role_arn: sess
    if redis_client is not None:
        mod._REDIS_FACTORY = lambda host, port, password, tls: redis_client
    if mc_stats is not None:
        mc = MagicMock()
        mc.stats.return_value = mc_stats
        mod._MEMCACHED_FACTORY = lambda host, port: mc


def test_redis_readonly_summary_and_allowlist():
    rc = _FakeRedis()
    _patch(None, redis_client=rc)
    r = mod.elasticache_live_read_impl(None, cluster_id="my-redis")
    assert r["status"] == "ok"
    assert r["info"]["memory"]["used_memory"] == 1024
    assert r["slowlog"][0]["duration_us"] == 12000
    assert len(r["slowlog"][0]["command"]) <= 130  # truncated
    assert r["clients"]["count"] == 2
    assert r["memory"]["total.allocated"] == 2048
    # READ-ONLY allowlist: only inspector methods were ever called
    called = {c[0] for c in rc.calls}
    assert called <= {"info", "slowlog_get", "client_list", "memory_stats"}


def test_memcached_path():
    row = {"region": "ap-northeast-2", "spoke_role_arn": "", "resource_name": "mc",
           "engine": "memcached",
           "resource_details": {"engine": "memcached", "tls_enabled": False, "auth_enabled": False}}
    _patch(None, mc_stats={b"curr_items": b"5", b"evictions": b"0", b"get_hits": b"100", b"get_misses": b"10"}, row=row)
    # cache-cluster endpoint resolution
    mod.session_for("x", "").client("elasticache").describe_replication_groups.side_effect = Exception("not rg")
    mod.session_for("x", "").client("elasticache").describe_cache_clusters.return_value = {"CacheClusters": [
        {"CacheClusterId": "mc", "ConfigurationEndpoint": {"Address": "mc.cache.amazonaws.com", "Port": 11211}}]}
    r = mod.elasticache_live_read_impl(None, cluster_id="mc")
    assert r["status"] == "ok" and r["engine"] == "memcached"
    assert r["memcached"]["curr_items"] == "5"


def test_missing_endpoint_unavailable():
    _patch(None, redis_client=_FakeRedis())
    ec = mod.session_for("x", "").client("elasticache")
    ec.describe_replication_groups.return_value = {"ReplicationGroups": []}
    ec.describe_cache_clusters.return_value = {"CacheClusters": []}
    r = mod.elasticache_live_read_impl(None, cluster_id="my-redis")
    assert r["status"] == "unavailable"


def test_connection_error_no_token_leak():
    class _Boom:
        def info(self, s): raise Exception("connection refused")
    _patch(None, redis_client=_Boom(), token="SUPERSECRET")
    r = mod.elasticache_live_read_impl(None, cluster_id="my-redis")
    assert r["status"] == "error"
    assert "SUPERSECRET" not in str(r)


def test_missing_cluster_id():
    assert mod.elasticache_live_read_impl(None)["status"] == "error"
