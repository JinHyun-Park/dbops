"""Unit tests for the simulation REST handler's DDL path.

Pin the REST mirror to the shared instance-derived-throughput model (no more
`row_count/100k*5`): time scales with table SIZE ÷ instance throughput, a
bigger instance is faster, and the response carries range/confidence/basis.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SIM_DIR = ROOT / "api" / "simulation"
HANDLER_PATH = SIM_DIR / "handler.py"
sys.path.insert(0, str(SIM_DIR))


def _load():
    spec = importlib.util.spec_from_file_location("simulation_handler", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["simulation_handler"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def mod():
    return _load()


def _wire(mod, monkeypatch, *, instance_class="db.r6g.large", size_mb=2000, row_count=5_000_000):
    table_bytes = int(size_mb * 1024 * 1024)

    def fake_cache_query(sql, params=None):
        if "table_stats" in sql:
            return [{"n_live_tup": row_count, "total_bytes": table_bytes}]
        if "cluster_meta" in sql:
            return [{"instance_class": instance_class}]
        return []

    monkeypatch.setattr(mod, "_cache_query", fake_cache_query)


def test_ddl_time_is_size_driven_not_rowcount_heuristic(mod, monkeypatch):
    _wire(mod, monkeypatch, size_mb=2000, row_count=5_000_000)
    out = mod._simulate_ddl_impact("c", "CREATE INDEX idx ON orders (x)")
    # old heuristic would be row_count/100k*5 = 250s; new model is size/throughput.
    assert out["estimated_seconds"] != 250
    assert out["operation"] == "create_index"
    lo, hi = out["estimated_range_seconds"]
    assert lo <= out["estimated_seconds"] <= hi
    assert out["confidence"] in ("low", "medium", "high")
    assert out["basis"]


def test_ddl_bigger_instance_is_faster(mod, monkeypatch):
    _wire(mod, monkeypatch, instance_class="db.r6g.large", size_mb=4000)
    small = mod._simulate_ddl_impact("c", "CREATE INDEX idx ON orders (x)")
    _wire(mod, monkeypatch, instance_class="db.r6g.16xlarge", size_mb=4000)
    big = mod._simulate_ddl_impact("c", "CREATE INDEX idx ON orders (x)")
    assert big["estimated_seconds"] < small["estimated_seconds"]


def test_ddl_metadata_only_fast(mod, monkeypatch):
    _wire(mod, monkeypatch, size_mb=8000)
    out = mod._simulate_ddl_impact("c", "ALTER TABLE users DROP COLUMN email")
    assert out["operation"] == "drop_column"
    assert out["estimated_seconds"] <= 5
    assert out["confidence"] == "high"
