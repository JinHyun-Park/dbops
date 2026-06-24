"""Unit tests for the simulation REST handler's `_simulate_elasticache_node_resize` path.

Pins the REST mirror's behaviour:
  - Happy path: current node type/count resolved from describe, prices from the
    Price List API, cost and delta computed correctly.
  - Pricing miss: status=partial, monthly costs None, never a fake figure.
  - Describe failure: status=partial, graceful response, no crash.
  - Dispatcher: /elasticache-node-resize route threads body fields through.
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
    spec = importlib.util.spec_from_file_location("simulation_handler_ec", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["simulation_handler_ec"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def mod():
    return _load()


def _ec_mock(node_type="cache.t4g.micro", member_count=2):
    ec = MagicMock()
    ec.describe_replication_groups.return_value = {
        "ReplicationGroups": [
            {
                "ReplicationGroupId": "my-redis",
                "CacheNodeType": node_type,
                "MemberClusters": [f"my-redis-{i:03d}" for i in range(member_count)],
            }
        ]
    }
    return ec


def _patch_env(mod, monkeypatch, region="ap-northeast-2"):
    monkeypatch.setattr(mod.os, "environ", {"AWS_REGION": region})


def _patch_cache(mod, monkeypatch, rows=None):
    """Stub _cache_query to return a cluster_meta row for the test cluster."""
    default = [{"engine": "redis", "region": "ap-northeast-2",
                "resource_name": "my-redis", "resource_details": None}]
    monkeypatch.setattr(mod, "_cache_query", lambda *a, **k: rows if rows is not None else default)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_node_resize_happy_path(mod, monkeypatch):
    _patch_env(mod, monkeypatch)
    _patch_cache(mod, monkeypatch)
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: _ec_mock("cache.t4g.micro", 2))
    monkeypatch.setattr(mod, "price_per_node_hour", lambda r, e, t: 0.017 if t == "cache.t4g.micro" else 0.182)

    result = mod._simulate_elasticache_node_resize("my-redis", new_node_type="cache.r7g.large")

    assert result["status"] == "ok"
    assert result["cluster_id"] == "my-redis"
    assert result["current"]["node_type"] == "cache.t4g.micro"
    assert result["current"]["node_count"] == 2
    assert result["proposed"]["node_type"] == "cache.r7g.large"
    assert result["proposed"]["node_count"] == 2
    # current: 0.017 * 2 * 730 = 24.82
    assert result["current_monthly"] == round(0.017 * 2 * 730, 2)
    # proposed: 0.182 * 2 * 730 = 265.72
    assert result["proposed_monthly"] == round(0.182 * 2 * 730, 2)
    assert result["delta_monthly"] == round(result["proposed_monthly"] - result["current_monthly"], 2)
    assert result["pricing_source"] == "aws_pricing_api"


def test_node_resize_describes_in_cluster_region(mod, monkeypatch):
    """The elasticache client must be region-scoped to the cluster's registered
    region (cluster_region), NOT the Lambda's default — else a cross-region
    cluster is described in the wrong region and mis-resolves to a misleading
    partial while pricing used the real region."""
    _patch_env(mod, monkeypatch, region="ap-northeast-2")  # Lambda default
    _patch_cache(mod, monkeypatch, rows=[{
        "engine": "redis", "region": "us-west-2",  # cluster registered elsewhere
        "resource_name": "my-redis", "resource_details": None}])
    captured = {}

    def _capture_client(*a, **k):
        captured["region_name"] = k.get("region_name")
        return _ec_mock("cache.t4g.micro", 2)

    monkeypatch.setattr(mod.boto3, "client", _capture_client)
    monkeypatch.setattr(mod, "price_per_node_hour", lambda r, e, t: 0.017 if t == "cache.t4g.micro" else 0.182)

    mod._simulate_elasticache_node_resize("my-redis", new_node_type="cache.r7g.large")

    assert captured["region_name"] == "us-west-2"


def test_node_count_change(mod, monkeypatch):
    _patch_env(mod, monkeypatch)
    _patch_cache(mod, monkeypatch)
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: _ec_mock("cache.r7g.large", 1))
    monkeypatch.setattr(mod, "price_per_node_hour", lambda *a, **k: 0.182)

    result = mod._simulate_elasticache_node_resize("my-redis", new_node_count=3)

    assert result["current"]["node_count"] == 1
    assert result["proposed"]["node_count"] == 3
    assert result["proposed"]["node_type"] == "cache.r7g.large"  # unchanged
    cur = round(0.182 * 1 * 730, 2)
    prop = round(0.182 * 3 * 730, 2)
    assert result["current_monthly"] == cur
    assert result["proposed_monthly"] == prop
    assert result["delta_monthly"] == round(prop - cur, 2)


def test_no_change_zero_delta(mod, monkeypatch):
    _patch_env(mod, monkeypatch)
    _patch_cache(mod, monkeypatch)
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: _ec_mock("cache.r7g.large", 2))
    monkeypatch.setattr(mod, "price_per_node_hour", lambda *a, **k: 0.182)

    result = mod._simulate_elasticache_node_resize("my-redis")

    assert result["status"] == "ok"
    assert result["delta_monthly"] == 0.0


# ---------------------------------------------------------------------------
# Pricing unavailable — status=partial, never fabricate
# ---------------------------------------------------------------------------

def test_pricing_miss_degrades_to_partial(mod, monkeypatch):
    _patch_env(mod, monkeypatch)
    _patch_cache(mod, monkeypatch)
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: _ec_mock())
    monkeypatch.setattr(mod, "price_per_node_hour", lambda *a, **k: None)

    result = mod._simulate_elasticache_node_resize("my-redis", new_node_type="cache.r7g.large")

    assert result["status"] == "partial"
    assert result["current_monthly"] is None
    assert result["proposed_monthly"] is None
    assert result["delta_monthly"] is None
    assert result["pricing_source"] == "fallback"


# ---------------------------------------------------------------------------
# Describe failure — graceful, no crash
# ---------------------------------------------------------------------------

def test_describe_failure_returns_partial(mod, monkeypatch):
    _patch_env(mod, monkeypatch)
    _patch_cache(mod, monkeypatch)
    ec = MagicMock()
    ec.describe_replication_groups.side_effect = RuntimeError("ReplicationGroupNotFoundFault")
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: ec)

    result = mod._simulate_elasticache_node_resize("ghost-cluster")

    assert result["status"] == "partial"
    assert "조회 실패" in result.get("note", "") or result.get("cluster_id") == "ghost-cluster"


# ---------------------------------------------------------------------------
# Dispatcher — /elasticache-node-resize route threads through body fields
# ---------------------------------------------------------------------------

def test_dispatcher_elasticache_node_resize(mod, monkeypatch):
    captured = {}

    def fake(cluster_id, new_node_type=None, new_node_count=None):
        captured["args"] = (cluster_id, new_node_type, new_node_count)
        return {"status": "ok"}

    monkeypatch.setattr(mod, "_simulate_elasticache_node_resize", fake)
    event = {
        "httpMethod": "POST",
        "rawPath": "/api/simulation/elasticache-node-resize",
        "body": '{"cluster_id": "my-redis", "new_node_type": "cache.r7g.large", "new_node_count": 3}',
    }
    mod.lambda_handler(event, None)
    assert captured["args"] == ("my-redis", "cache.r7g.large", 3)
