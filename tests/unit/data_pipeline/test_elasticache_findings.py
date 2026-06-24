"""ElastiCache findings collector → cluster_health_findings."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_C = Path(__file__).resolve().parents[3] / "data-pipeline/etl_collector/collectors/elasticache_findings.py"
_spec = importlib.util.spec_from_file_location("ec_findings", _C)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _fake_rds(meta_engine="redis", agg=None):
    """Mock rds_data so the FIRST execute_statement (cluster_meta engine) returns
    the engine, the SECOND (metric aggregation) returns agg, and INSERTs record."""
    rds = MagicMock()
    inserts = []
    calls = {"n": 0}
    agg = agg or {}

    def _exec(**kwargs):
        sql = kwargs.get("sql", "")
        if "FROM cluster_meta" in sql:
            return {"columnMetadata": [{"name": "engine"}],
                    "records": [[{"stringValue": meta_engine}]]}
        if "INSERT INTO cluster_health_findings" in sql:
            inserts.append({p["name"]: list(p["value"].values())[0] for p in kwargs.get("parameters", [])})
            return {"columnMetadata": [], "records": []}
        # aggregation query
        cols = list(agg.keys())
        rec = [({"isNull": True} if agg[c] is None else {"doubleValue": float(agg[c])}) for c in cols]
        return {"columnMetadata": [{"name": c} for c in cols], "records": [rec]}

    rds.execute_statement.side_effect = _exec
    rds._inserts = inserts
    return rds


def _run(rds):
    return mod.collect_elasticache_findings(rds, "arn", "sec", "db", "my-redis", snapshot_ts="2026-06-24T00:00:00Z")


def test_evictions_spike_critical():
    rds = _fake_rds("redis", {"sum_evictions": 1500, "sum_cache_hits": 900, "sum_cache_misses": 100,
                              "max_memory_pct": 10, "max_replication_lag": 0, "max_engine_cpu": 5,
                              "max_cache_cpu": 5, "max_curr_connections": 10, "hit_samples": 30})
    _run(rds)
    types = {i["check_type"]: i["severity"] for i in rds._inserts}
    assert types.get("elasticache_evictions_spike") == "critical"


def test_low_hit_rate_warning_and_shared_ts():
    rds = _fake_rds("redis", {"sum_evictions": 0, "sum_cache_hits": 80, "sum_cache_misses": 20,
                              "max_memory_pct": 10, "max_replication_lag": 0, "max_engine_cpu": 5,
                              "max_cache_cpu": 5, "max_curr_connections": 10, "hit_samples": 30})
    _run(rds)
    ins = {i["check_type"]: i for i in rds._inserts}
    assert "elasticache_low_hit_rate" in ins  # 80% < 85% warning
    # every finding shares the passed snapshot_ts
    assert all(i["ts"] == "2026-06-24T00:00:00Z" for i in rds._inserts)


def test_memory_and_lag_critical_redis():
    rds = _fake_rds("redis", {"sum_evictions": 0, "sum_cache_hits": 100, "sum_cache_misses": 0,
                              "max_memory_pct": 97, "max_replication_lag": 1500, "max_engine_cpu": 5,
                              "max_cache_cpu": 5, "max_curr_connections": 10, "hit_samples": 30})
    _run(rds)
    types = {i["check_type"]: i["severity"] for i in rds._inserts}
    assert types.get("elasticache_memory_pressure") == "critical"
    assert types.get("elasticache_replication_lag") == "critical"


def test_memcached_skips_lag_and_memory_uses_get_hits():
    # Memcached: replication_lag + memory_pressure rules skipped; hit-rate from get_hits/get_misses
    rds = _fake_rds("memcached", {"sum_evictions": 0, "sum_get_hits": 50, "sum_get_misses": 50,
                                  "max_memory_pct": 99, "max_replication_lag": 9999, "max_engine_cpu": 5,
                                  "max_cache_cpu": 5, "max_curr_connections": 10, "hit_samples": 30})
    _run(rds)
    types = {i["check_type"] for i in rds._inserts}
    assert "elasticache_replication_lag" not in types
    assert "elasticache_memory_pressure" not in types
    assert "elasticache_low_hit_rate" in types  # 50% hit-rate via get_hits


def test_high_cpu_warning():
    rds = _fake_rds("redis", {"sum_evictions": 0, "sum_cache_hits": 100, "sum_cache_misses": 0,
                              "max_memory_pct": 10, "max_replication_lag": 0, "max_engine_cpu": 85,
                              "max_cache_cpu": 50, "max_curr_connections": 10, "hit_samples": 30})
    _run(rds)
    types = {i["check_type"]: i["severity"] for i in rds._inserts}
    assert types.get("elasticache_high_cpu") == "warning"


def test_no_findings_when_healthy():
    rds = _fake_rds("redis", {"sum_evictions": 0, "sum_cache_hits": 1000, "sum_cache_misses": 0,
                              "max_memory_pct": 10, "max_replication_lag": 0, "max_engine_cpu": 5,
                              "max_cache_cpu": 5, "max_curr_connections": 10, "hit_samples": 30})
    res = _run(rds)
    assert res["findings_emitted"] == 0


def test_low_hit_rate_skipped_below_min_samples():
    rds = _fake_rds("redis", {"sum_evictions": 0, "sum_cache_hits": 1, "sum_cache_misses": 9,
                              "max_memory_pct": 10, "max_replication_lag": 0, "max_engine_cpu": 5,
                              "max_cache_cpu": 5, "max_curr_connections": 10, "hit_samples": 5})
    _run(rds)
    assert "elasticache_low_hit_rate" not in {i["check_type"] for i in rds._inserts}


def test_connection_surge_warning():
    rds = _fake_rds("redis", {"sum_evictions": 0, "sum_cache_hits": 1000, "sum_cache_misses": 0,
                              "max_memory_pct": 10, "max_replication_lag": 0, "max_engine_cpu": 5,
                              "max_cache_cpu": 5, "max_curr_connections": 70000, "hit_samples": 30})
    _run(rds)
    types = {i["check_type"]: i["severity"] for i in rds._inserts}
    assert types.get("elasticache_connection_surge") == "warning"
