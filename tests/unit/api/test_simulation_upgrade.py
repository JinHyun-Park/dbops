"""Unit tests for the simulation REST handler's upgrade paths.

Pin the REST mirror to the same object-count-driven model the MCP tool +
frontend depend on:
  - estimated time is NOT the old storage*coeff / len(steps)*5 heuristic.
  - MAJOR time scales with the live table_stats OBJECT COUNT, not storage.
  - blue/green downtime is sub-minute; the response carries range +
    confidence + methodology note.
  - recommendation is data-driven (major -> blue_green), not a constant.

No real AWS calls: _cache_query and the RDS describe are stubbed.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

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


def _wire(mod, monkeypatch, *, engine="aurora-postgresql", version="15.4",
          storage_gb=200, table_count=3000, readers=0):
    """Stub _cache_query (meta + table-count) and the RDS reader describe."""

    def fake_cache_query(sql, params=None):
        if "cluster_meta" in sql:
            return [{"engine": engine, "engine_version": version, "storage_size_gb": storage_gb}]
        if "table_stats" in sql:
            return [] if table_count is None else [{"n": table_count}]
        return []

    monkeypatch.setattr(mod, "_cache_query", fake_cache_query)

    rds = MagicMock()
    members = [{"IsClusterWriter": True}] + [{"IsClusterWriter": False}] * readers
    rds.describe_db_clusters.return_value = {"DBClusters": [{"DBClusterMembers": members}]}
    fake_boto = MagicMock()
    fake_boto.client.return_value = rds
    monkeypatch.setattr(mod, "boto3", fake_boto)


# --- estimate_upgrade_impact --------------------------------------------------


def test_impact_major_recommends_blue_green_with_ranges(mod, monkeypatch):
    _wire(mod, monkeypatch, version="15.4", table_count=3000)
    out = mod._estimate_upgrade_impact("prod-pg-1", "16.2")

    assert out["upgrade_type"] == "major"
    assert out["recommendation"] == "blue_green"  # not the old constant-by-accident
    assert len(out["methods"]) == 3
    for m in out["methods"]:
        assert m["range_low_minutes"] <= m["estimated_minutes"] <= m["range_high_minutes"]
        assert "downtime_text" in m and "downtime_seconds" in m
    assert out["confidence"] == "medium"
    assert out["table_count"] == 3000
    assert out["methodology_note"]


def test_impact_major_time_scales_with_object_count(mod, monkeypatch):
    _wire(mod, monkeypatch, table_count=500)
    few = mod._estimate_upgrade_impact("c", "16.2")
    _wire(mod, monkeypatch, table_count=50000)
    many = mod._estimate_upgrade_impact("c", "16.2")

    few_ip = next(m for m in few["methods"] if m["method"] == "in_place")
    many_ip = next(m for m in many["methods"] if m["method"] == "in_place")
    assert many_ip["estimated_minutes"] > few_ip["estimated_minutes"]


def test_impact_blue_green_downtime_subminute(mod, monkeypatch):
    _wire(mod, monkeypatch, version="12.4", storage_gb=8000, table_count=100000, readers=4)
    out = mod._estimate_upgrade_impact("c", "16.2")
    bg = next(m for m in out["methods"] if m["method"] == "blue_green")
    assert bg["downtime_seconds"] <= 60


def test_impact_missing_table_stats_is_low_confidence(mod, monkeypatch):
    _wire(mod, monkeypatch, table_count=None)
    out = mod._estimate_upgrade_impact("c", "16.2")
    assert out["upgrade_type"] == "major"
    assert out["confidence"] == "low"
    assert out["table_count"] is None


# --- generate_upgrade_plan ----------------------------------------------------


def test_plan_time_is_not_len_steps_times_five(mod, monkeypatch):
    _wire(mod, monkeypatch, version="15.4", storage_gb=800, table_count=20000, readers=2)
    out = mod._generate_upgrade_plan("prod-pg-1", "16.2", "blue_green")
    assert out["estimated_total_minutes"] != len(out["steps"]) * 5
    assert out["estimated_range_minutes"][0] <= out["estimated_total_minutes"] <= out["estimated_range_minutes"][1]
    assert out["downtime_text"]
    assert out["confidence"] in ("low", "medium", "high")
    assert out["upgrade_type"] == "major"
    assert out["methodology_note"]


def test_plan_minor_cheaper_than_major(mod, monkeypatch):
    _wire(mod, monkeypatch, version="15.4", table_count=2000)
    minor = mod._generate_upgrade_plan("c", "15.7", "in_place")
    _wire(mod, monkeypatch, version="15.4", table_count=2000)
    major = mod._generate_upgrade_plan("c", "16.2", "in_place")
    assert minor["estimated_total_minutes"] < major["estimated_total_minutes"]
