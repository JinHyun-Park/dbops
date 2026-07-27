"""HealthScore signal sets must reference metric_types the collectors write.

health-score.tsx scores a cluster against a per-family signal list. A signal
naming a metric_type nobody collects renders a permanently blank row (and used
to score rds_instance against Aurora-only metrics), so this test pins the TSX
signal metrics to the collector sources. Regex-based on purpose: no JS runtime
in CI, and the shapes on both sides are flat literal lists.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TSX = (_ROOT / "frontend/src/components/dashboard/health-score.tsx").read_text()
_RDS_COLLECTOR = (
    _ROOT / "data-pipeline/etl_collector/collectors/rds_instance_cw_collector.py"
).read_text()
_EC_COLLECTOR = (
    _ROOT / "data-pipeline/etl_collector/collectors/elasticache_cw_collector.py"
).read_text()

# ("CPUUtilization", "cpu", "Average") -> "cpu"
_TUPLE_RE = re.compile(r'\(\s*"[^"]+"\s*,\s*"([^"]+)"\s*,\s*"[^"]+"\s*\)')


def _py_list(src: str, name: str) -> set:
    block = re.search(rf"^{name} = \[(.*?)^\]", src, re.S | re.M)
    assert block, f"{name} not found in collector source"
    return set(_TUPLE_RE.findall(block.group(1)))


def _tsx_block(name: str) -> str:
    block = re.search(rf"^const {name}: SignalDef\[\] = \[(.*?)^\];", _TSX, re.S | re.M)
    assert block, f"{name} not found in health-score.tsx"
    return block.group(1)


def _tsx_metrics(name: str) -> set:
    return set(re.findall(r'metric:\s*"([^"]+)"', _tsx_block(name)))


def _tsx_weight_sum(name: str) -> int:
    return sum(int(w) for w in re.findall(r"weight:\s*(\d+)", _tsx_block(name)))


def test_rds_instance_signals_are_collected():
    collected = _py_list(_RDS_COLLECTOR, "_METRICS")
    signals = _tsx_metrics("SIGNALS_RDS_INSTANCE")
    assert signals, "SIGNALS_RDS_INSTANCE is empty"
    assert signals <= collected, f"not collected for rds_instance: {signals - collected}"
    # Aurora-only metrics were the bug: never published for a standalone instance.
    assert not signals & {"replica_lag_ms", "deadlocks", "buffer_cache_hit", "aas"}


def test_elasticache_signals_are_collected_per_engine():
    redis = _py_list(_EC_COLLECTOR, "_REDIS_METRICS")
    memcached = _py_list(_EC_COLLECTOR, "_MEMCACHED_METRICS")
    r_sig = _tsx_metrics("SIGNALS_ELASTICACHE_REDIS")
    m_sig = _tsx_metrics("SIGNALS_ELASTICACHE_MEMCACHED")
    assert r_sig and m_sig
    assert r_sig <= redis, f"not collected for redis/valkey: {r_sig - redis}"
    assert m_sig <= memcached, f"not collected for memcached: {m_sig - memcached}"
    # Redis-only metrics must not leak into the Memcached set.
    assert not m_sig & {"engine_cpu", "memory_usage_pct", "replication_lag"}
    # 'cpu' is relational; ElastiCache publishes cache_cpu / engine_cpu.
    assert "cpu" not in r_sig | m_sig


def test_signal_weights_sum_to_100():
    for name in (
        "SIGNALS_RELATIONAL",
        "SIGNALS_DOCUMENTDB",
        "SIGNALS_DYNAMODB",
        "SIGNALS_RDS_INSTANCE",
        "SIGNALS_ELASTICACHE_REDIS",
        "SIGNALS_ELASTICACHE_MEMCACHED",
    ):
        assert _tsx_weight_sum(name) == 100, name


def test_scalar_signals_read_the_cluster_level_row_only():
    """/batch-timeseries returns DIMENSIONED rows NEXT TO the cluster-level one
    (`GROUP BY 1, metric_type, dimensions::text`) because the stacked charts need
    the breakdown: `aas` is written once as the cluster total (dimensions '{}')
    PLUS one row per Performance Insights wait event at the SAME timestamp, and
    DynamoDB throttles are written per table AND per GSI.

    HealthScore samples a SCALAR per signal, so taking the last element of the
    series could score "Load (AAS)" from a single wait event's fraction, and the
    health GRADE (worst-signal) moved with whichever row sorted last. The API
    must keep returning the dimensioned rows, so the filter belongs here.
    `dimensions` is `dimensions::text`: None -> null (legacy rows) or '{}'.
    """
    pred = re.search(r"const isClusterLevel\s*=\s*\(p:[^)]*\)\s*=>\s*(.+?);", _TSX, re.S)
    assert pred, "cluster-level predicate isClusterLevel missing from health-score.tsx"
    body = " ".join(pred.group(1).split())
    assert "p.dimensions == null" in body, body
    assert 'p.dimensions === "{}"' in body, body

    # The signal loop must sample THROUGH the filter, not the raw series.
    loop = re.search(r"const points = (.+?);", _TSX)
    assert loop and loop.group(1).startswith("clusterLevelPoints("), (
        "HealthScore must sample clusterLevelPoints(...): the raw series mixes "
        "the cluster total with its per-wait-event / per-GSI rows"
    )
    # Fallback is load-bearing: DynamoDB latency_ms_* is ONLY ever published with
    # {"operation": ...} dimensions, so a bare filter would blank those signals.
    assert "cl.length ? cl : points" in _TSX


def test_signals_for_engine_branches_every_family():
    fn = re.search(r"function signalsForEngine\(.*?\n}", _TSX, re.S)
    assert fn
    body = fn.group(0)
    for fam in ("documentdb", "dynamodb", "rds_instance", "elasticache"):
        assert f'fam === "{fam}"' in body, fam
    assert "memcached" in body, "Memcached must not be scored with the Redis set"
