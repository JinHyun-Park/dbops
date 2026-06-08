"""Unit tests for the simulation REST handler's `_simulate_scaling` path.

These pin the REST mirror's behaviour against the shared contract the MCP tool
+ frontend depend on:
  - Serverless v2 uses the REAL ACU price (0.26, not the old hardcoded 0.12)
    times midpoint times member count.
  - Provisioned clusters price by instance class and support a resize via
    new_instance_class.
  - A pricing miss degrades to a cost-free estimate (source "fallback"),
    never a crash and never fabricated numbers.

No real AWS calls: the RDS client and both pricing helpers are mocked.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
SIM_DIR = ROOT / "api" / "simulation"
HANDLER_PATH = SIM_DIR / "handler.py"

# The handler does `from aurora_pricing import ...` (a sibling module); at
# Lambda runtime the function root is on sys.path, so mirror that for the test.
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


def _serverless_cluster(min_capacity=2.0, max_capacity=16.0, readers=1, io=False):
    members = [{"IsClusterWriter": True, "DBInstanceIdentifier": "writer-1"}]
    for i in range(readers):
        members.append({"IsClusterWriter": False, "DBInstanceIdentifier": f"reader-{i}"})
    return {
        "DBClusterIdentifier": "prod-pg-1",
        "Engine": "aurora-postgresql",
        "StorageType": "aurora-iopt1" if io else "aurora",
        "ServerlessV2ScalingConfiguration": {
            "MinCapacity": min_capacity,
            "MaxCapacity": max_capacity,
        },
        "DBClusterMembers": members,
    }


def _provisioned_cluster(readers=1, io=False):
    members = [{"IsClusterWriter": True, "DBInstanceIdentifier": "writer-1"}]
    for i in range(readers):
        members.append({"IsClusterWriter": False, "DBInstanceIdentifier": f"reader-{i}"})
    return {
        "DBClusterIdentifier": "prod-pg-1",
        "Engine": "aurora-postgresql",
        "StorageType": "aurora-iopt1" if io else "aurora",
        "DBClusterMembers": members,
    }


def _rds_mock(cluster, instances=None):
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [cluster]}
    rds.describe_db_instances.return_value = {"DBInstances": instances or []}
    return rds


def _patch_env_region(mod, monkeypatch):
    monkeypatch.setattr(mod.os, "environ", {"AWS_REGION": "ap-northeast-2"})


# ---------------------------------------------------------------------------
# Serverless v2 — REAL ACU price, midpoint, member count
# ---------------------------------------------------------------------------


def test_serverless_uses_real_acu_price_not_hardcoded_012(mod, monkeypatch):
    """Cost must use the live 0.26 ACU price, NOT the old hardcoded 0.12."""
    _patch_env_region(mod, monkeypatch)
    rds = _rds_mock(_serverless_cluster(min_capacity=2.0, max_capacity=16.0, readers=1, io=True))
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: rds)
    monkeypatch.setattr(mod, "price_per_acu_hour", lambda *a, **k: 0.26)

    result = mod._simulate_scaling("prod-pg-1")

    assert result["mode"] == "serverless"
    assert result["current"] == {"min_acu": 2.0, "max_acu": 16.0}
    assert result["writers"] == 1
    assert result["readers"] == 1
    members = 2
    expected = ((2.0 + 16.0) / 2) * 0.26 * 730 * members
    assert result["cost_impact"]["current_monthly_usd"] == round(expected, 2)
    # The old 0.12 figure must NOT appear.
    wrong = ((2.0 + 16.0) / 2) * 0.12 * 730 * members
    assert result["cost_impact"]["current_monthly_usd"] != round(wrong, 2)
    assert result["unit_pricing"] == {
        "kind": "acu",
        "price_per_hour": 0.26,
        "region": "ap-northeast-2",
        "io_optimized": True,
        "source": "aws_pricing_api",
    }


def test_serverless_proposed_defaults_to_current_zero_delta(mod, monkeypatch):
    _patch_env_region(mod, monkeypatch)
    rds = _rds_mock(_serverless_cluster())
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: rds)
    monkeypatch.setattr(mod, "price_per_acu_hour", lambda *a, **k: 0.26)

    result = mod._simulate_scaling("prod-pg-1")

    assert result["proposed"] == {"min_acu": 2.0, "max_acu": 16.0}
    assert result["cost_impact"]["delta_monthly_usd"] == 0.0
    assert result["cost_impact"]["change_pct"] == 0.0


def test_serverless_resize_changes_cost_and_pct(mod, monkeypatch):
    _patch_env_region(mod, monkeypatch)
    rds = _rds_mock(_serverless_cluster(readers=1))
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: rds)
    monkeypatch.setattr(mod, "price_per_acu_hour", lambda *a, **k: 0.26)

    result = mod._simulate_scaling("prod-pg-1", new_min_acu=4.0, new_max_acu=32.0)

    members = 2
    cur = ((2.0 + 16.0) / 2) * 0.26 * 730 * members
    prop = ((4.0 + 32.0) / 2) * 0.26 * 730 * members
    assert result["proposed"] == {"min_acu": 4.0, "max_acu": 32.0}
    assert result["cost_impact"]["proposed_monthly_usd"] == round(prop, 2)
    assert result["cost_impact"]["delta_monthly_usd"] == round(prop - cur, 2)
    # Doubling the midpoint -> +100%.
    assert result["cost_impact"]["change_pct"] == 100.0


# ---------------------------------------------------------------------------
# Provisioned — instance class pricing + resize via new_instance_class
# ---------------------------------------------------------------------------


def test_provisioned_current_cost_sums_member_instance_prices(mod, monkeypatch):
    _patch_env_region(mod, monkeypatch)
    instances = [
        {"DBInstanceClass": "db.r6g.large"},
        {"DBInstanceClass": "db.r6g.large"},
    ]
    rds = _rds_mock(_provisioned_cluster(readers=1), instances=instances)
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: rds)
    monkeypatch.setattr(mod, "price_per_instance_hour", lambda *a, **k: 0.29)

    result = mod._simulate_scaling("prod-pg-1")

    assert result["mode"] == "provisioned"
    assert result["current"] == {"instance_class": "db.r6g.large"}
    assert result["proposed"] == {"instance_class": "db.r6g.large"}
    # 2 members * 0.29 * 730
    expected = (0.29 + 0.29) * 730
    assert result["cost_impact"]["current_monthly_usd"] == round(expected, 2)
    # No resize -> proposed == current -> 0 delta.
    assert result["cost_impact"]["proposed_monthly_usd"] == round(expected, 2)
    assert result["cost_impact"]["delta_monthly_usd"] == 0.0
    assert result["unit_pricing"]["kind"] == "instance"
    assert result["unit_pricing"]["source"] == "aws_pricing_api"


def test_provisioned_resize_with_new_instance_class(mod, monkeypatch):
    _patch_env_region(mod, monkeypatch)
    instances = [
        {"DBInstanceClass": "db.r6g.large"},
        {"DBInstanceClass": "db.r6g.large"},
    ]
    rds = _rds_mock(_provisioned_cluster(readers=1), instances=instances)
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: rds)

    prices = {"db.r6g.large": 0.29, "db.r6g.xlarge": 0.58}

    def fake_price(region, engine, cls, io):
        return prices[cls]

    monkeypatch.setattr(mod, "price_per_instance_hour", fake_price)

    result = mod._simulate_scaling("prod-pg-1", new_instance_class="db.r6g.xlarge")

    assert result["proposed"] == {"instance_class": "db.r6g.xlarge"}
    cur = (0.29 + 0.29) * 730
    prop = 2 * 0.58 * 730  # member_count * proposed price * hours
    assert result["cost_impact"]["current_monthly_usd"] == round(cur, 2)
    assert result["cost_impact"]["proposed_monthly_usd"] == round(prop, 2)
    assert result["cost_impact"]["delta_monthly_usd"] == round(prop - cur, 2)
    assert result["cost_impact"]["change_pct"] == 100.0
    assert result["unit_pricing"]["price_per_hour"] == 0.58


# ---------------------------------------------------------------------------
# Pricing unavailable — degrade to estimate, never crash, never fabricate
# ---------------------------------------------------------------------------


def test_serverless_pricing_none_degrades_to_fallback(mod, monkeypatch):
    _patch_env_region(mod, monkeypatch)
    rds = _rds_mock(_serverless_cluster())
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: rds)
    monkeypatch.setattr(mod, "price_per_acu_hour", lambda *a, **k: None)

    result = mod._simulate_scaling("prod-pg-1")

    assert result["cost_impact"] == {
        "current_monthly_usd": None,
        "proposed_monthly_usd": None,
        "delta_monthly_usd": None,
        "change_pct": None,
    }
    assert result["unit_pricing"]["price_per_hour"] is None
    assert result["unit_pricing"]["source"] == "fallback"
    assert result["data_source"] == "estimate (pricing unavailable)"
    # Current ACU range is still surfaced from the live describe.
    assert result["current"] == {"min_acu": 2.0, "max_acu": 16.0}


def test_provisioned_pricing_none_degrades_to_fallback(mod, monkeypatch):
    _patch_env_region(mod, monkeypatch)
    instances = [{"DBInstanceClass": "db.r6g.large"}]
    rds = _rds_mock(_provisioned_cluster(readers=0), instances=instances)
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: rds)
    monkeypatch.setattr(mod, "price_per_instance_hour", lambda *a, **k: None)

    result = mod._simulate_scaling("prod-pg-1")

    assert result["mode"] == "provisioned"
    assert result["cost_impact"]["current_monthly_usd"] is None
    assert result["cost_impact"]["proposed_monthly_usd"] is None
    assert result["unit_pricing"]["source"] == "fallback"
    assert result["data_source"] == "estimate (pricing unavailable)"


# ---------------------------------------------------------------------------
# Dispatcher — new_instance_class threads through /scaling
# ---------------------------------------------------------------------------


def test_lambda_handler_passes_new_instance_class(mod, monkeypatch):
    captured = {}

    def fake_sim(cluster_id, new_min=None, new_max=None, new_instance_class=None):
        captured["args"] = (cluster_id, new_min, new_max, new_instance_class)
        return {"ok": True}

    monkeypatch.setattr(mod, "_simulate_scaling", fake_sim)
    event = {
        "httpMethod": "POST",
        "rawPath": "/api/simulation/scaling",
        "body": (
            '{"cluster_id": "prod-pg-1", "new_min_acu": 4, '
            '"new_max_acu": 32, "new_instance_class": "db.r6g.xlarge"}'
        ),
    }
    mod.lambda_handler(event, None)
    assert captured["args"] == ("prod-pg-1", 4, 32, "db.r6g.xlarge")


def test_describe_failure_degrades_gracefully_no_crash(mod, monkeypatch):
    """An RDS describe failure must NOT crash — return a cost-free estimate that
    still matches the contract (the 'never crash' promise)."""
    _patch_env_region(mod, monkeypatch)
    rds = MagicMock()
    rds.describe_db_clusters.side_effect = RuntimeError("DBClusterNotFoundFault")
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: rds)

    result = mod._simulate_scaling("ghost-cluster", new_instance_class="db.r6g.large")

    assert result["data_source"].startswith("estimate")
    assert result["cost_impact"]["current_monthly_usd"] is None
    assert result["unit_pricing"]["source"] == "fallback"
    assert result["mode"] == "provisioned"  # inferred from new_instance_class
