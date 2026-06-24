"""All four engine_family.py copies must classify ElastiCache identically."""
import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_COPIES = [
    _ROOT / "api/clusters/engine_family.py",
    _ROOT / "api/dashboard/engine_family.py",
    _ROOT / "data-pipeline/etl_collector/collectors/engine_family.py",
    _ROOT / "mcp-servers/mcp_servers/shared/engine_family.py",
]


def _load(p):
    spec = importlib.util.spec_from_file_location(f"ef_{abs(hash(str(p)))}", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_all_copies_classify_elasticache():
    for p in _COPIES:
        m = _load(p)
        assert m.engine_family("redis") == "elasticache"
        assert m.engine_family("valkey") == "elasticache"
        assert m.engine_family("memcached") == "elasticache"
        assert m.engine_family("elasticache-redis") == "elasticache"
        # unchanged families
        assert m.engine_family("aurora-postgresql") == "relational"
        assert m.engine_family("docdb") == "documentdb"
        assert m.engine_family("dynamodb") == "dynamodb"
        caps = m.CAPABILITIES["elasticache"]
        assert caps["sql"] is False
        assert caps["cw_namespace"] == "AWS/ElastiCache"
        assert caps["elasticache_write"] is True
        assert caps["live_read"] is True
        assert caps["findings"] == {"elasticache"}
        assert caps["rds_meta"] is False
        assert caps["perf_insights"] is False
