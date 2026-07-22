"""Unit tests for the simulation REST handler's `_simulate_rds_instance_rightsizing`.

Pins the REST mirror's behaviour against the reviewed MCP tool
(mcp_servers/simulation/tools/rds_rightsizing.py):
  - Downsize on low CPU + connection headroom, with a real cheaper cost and
    pricing_source="aws_price_list".
  - SQL Server: the pricing fn is called WITHOUT edition="sqlserver-ex" — the
    pricing helper resolves databaseEdition from the engine itself.
  - A non-rds_instance engine → status="unsupported_engine".
  - A null unit price → pricing_source="fallback_estimate" and null cost fields
    (never a fabricated figure).
  - Dispatcher: /rds-instance-rightsizing threads body fields through.

_cache_query and the two pricing functions are mocked; never hits AWS.
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
    spec = importlib.util.spec_from_file_location("simulation_handler_rds", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["simulation_handler_rds"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def mod():
    return _load()


def _default_meta(engine="mysql", instance_class="db.r6g.large"):
    return [{
        "engine": engine,
        "instance_class": instance_class,
        "region": "ap-northeast-2",
        "resource_details": {
            "allocated_storage_gb": 100,
            "storage_type": "gp3",
            "multi_az": False,
        },
    }]


def _default_agg(cpu_p95=12.0, conn_peak=10, samples=200):
    return [{
        "cpu_p95": cpu_p95,
        "cpu_avg": 8.0,
        "conn_peak": conn_peak,
        "read_iops_p95": 100.0,
        "write_iops_p95": 50.0,
        "freeable_mem_min": 2 * 1024 * 1024 * 1024,
        "samples": samples,
    }]


def _patch_env(mod, monkeypatch, region="ap-northeast-2"):
    monkeypatch.setattr(mod.os, "environ", {"AWS_REGION": region})


def _patch_cache(mod, monkeypatch, meta=None, agg=None):
    """Route _cache_query by SQL: cluster_meta read vs metric aggregation."""
    meta = _default_meta() if meta is None else meta
    agg = _default_agg() if agg is None else agg

    def _q(sql, params=None):
        return meta if "cluster_meta" in sql else agg

    monkeypatch.setattr(mod, "_cache_query", _q)


# ---------------------------------------------------------------------------
# Downsize happy path — real cheaper cost, aws_price_list
# ---------------------------------------------------------------------------

def test_downsize_low_cpu_real_cheaper_cost(mod, monkeypatch):
    _patch_env(mod, monkeypatch)
    _patch_cache(mod, monkeypatch)
    monkeypatch.setattr(
        mod, "price_rds_instance_hour",
        lambda region, engine, cls, **k: 0.25 if cls == "db.r6g.large" else 0.12,
    )
    monkeypatch.setattr(
        mod, "price_rds_storage_month",
        lambda *a, **k: {"storage_usd": 11.5, "iops_usd": 0.0},
    )

    result = mod._simulate_rds_instance_rightsizing("rds-mysql-1")

    assert result["status"] == "ok"
    assert result["cluster_id"] == "rds-mysql-1"
    assert result["engine"] == "mysql"
    assert result["current"]["instance_class"] == "db.r6g.large"
    assert result["recommendation"]["action"] == "downsize"
    assert result["recommendation"]["instance_class"] == "db.r6g.medium"
    # cur: 0.25*730 + 11.5 = 194.0 ; tgt: 0.12*730 + 11.5 = 99.1
    assert result["cost_impact"]["current_monthly_usd"] == round(0.25 * 730 + 11.5, 2)
    assert result["cost_impact"]["proposed_monthly_usd"] == round(0.12 * 730 + 11.5, 2)
    assert result["cost_impact"]["delta_monthly_usd"] < 0
    assert result["cost_impact"]["pricing_source"] == "aws_price_list"
    assert result["utilization"]["samples"] == 200


# ---------------------------------------------------------------------------
# SQL Server: pricing fn must NOT receive edition="sqlserver-ex"
# ---------------------------------------------------------------------------

def test_sqlserver_edition_not_passed_as_engine_string(mod, monkeypatch):
    _patch_env(mod, monkeypatch)
    _patch_cache(mod, monkeypatch, meta=_default_meta(engine="sqlserver-ex",
                                                      instance_class="db.m5.large"))
    calls = []

    def _price(region, engine, cls, edition=None, multi_az=False):
        calls.append({"engine": engine, "cls": cls, "edition": edition})
        return 0.40

    monkeypatch.setattr(mod, "price_rds_instance_hour", _price)
    monkeypatch.setattr(
        mod, "price_rds_storage_month",
        lambda *a, **k: {"storage_usd": 11.5, "iops_usd": 0.0},
    )

    result = mod._simulate_rds_instance_rightsizing("rds-sqlserver-1")

    assert result["status"] == "ok"
    assert result["cost_impact"]["pricing_source"] == "aws_price_list"
    # The engine string is passed as `engine`, and edition is left None so the
    # pricing helper resolves the Price-List databaseEdition itself. Passing
    # "sqlserver-ex" as `edition` would match zero SKUs.
    assert calls, "price_rds_instance_hour was not called"
    for c in calls:
        assert c["engine"] == "sqlserver-ex"
        assert c["edition"] is None
    assert result["cost_impact"]["breakdown"]["license_note"]  # SQL Server note present


# ---------------------------------------------------------------------------
# Unsupported engine — positive guard
# ---------------------------------------------------------------------------

def test_unsupported_engine_refused(mod, monkeypatch):
    _patch_env(mod, monkeypatch)
    _patch_cache(mod, monkeypatch, meta=_default_meta(engine="aurora-postgresql"))
    monkeypatch.setattr(mod, "price_rds_instance_hour", lambda *a, **k: 0.25)
    monkeypatch.setattr(mod, "price_rds_storage_month",
                        lambda *a, **k: {"storage_usd": 1.0, "iops_usd": 0.0})

    result = mod._simulate_rds_instance_rightsizing("aurora-pg-1")

    assert result["status"] == "unsupported_engine"
    assert result["cluster_id"] == "aurora-pg-1"
    assert result.get("message")


# ---------------------------------------------------------------------------
# Null price → fallback_estimate, never fabricate
# ---------------------------------------------------------------------------

def test_null_price_degrades_to_fallback(mod, monkeypatch):
    _patch_env(mod, monkeypatch)
    _patch_cache(mod, monkeypatch)
    monkeypatch.setattr(mod, "price_rds_instance_hour", lambda *a, **k: None)
    monkeypatch.setattr(mod, "price_rds_storage_month",
                        lambda *a, **k: {"storage_usd": None, "iops_usd": None})

    result = mod._simulate_rds_instance_rightsizing("rds-mysql-1")

    assert result["status"] == "ok"
    assert result["cost_impact"]["pricing_source"] == "fallback_estimate"
    assert result["cost_impact"]["current_monthly_usd"] is None
    assert result["cost_impact"]["proposed_monthly_usd"] is None
    assert result["cost_impact"]["delta_monthly_usd"] is None


# ---------------------------------------------------------------------------
# Missing cluster_meta row → error, no leak
# ---------------------------------------------------------------------------

def test_missing_meta_returns_error(mod, monkeypatch):
    _patch_env(mod, monkeypatch)
    _patch_cache(mod, monkeypatch, meta=[])

    result = mod._simulate_rds_instance_rightsizing("ghost")

    assert result["status"] == "error"
    assert result["cluster_id"] == "ghost"
    assert "reason" in result


# ---------------------------------------------------------------------------
# Explicit override: action label follows the cost delta sign, not hardcoded
# ---------------------------------------------------------------------------

def test_override_action_from_cost_delta(mod, monkeypatch):
    _patch_env(mod, monkeypatch)
    _patch_cache(mod, monkeypatch, agg=_default_agg(cpu_p95=55.0))
    # Requested a bigger class but it happens to be cheaper here → action must
    # read "downsize" (delta<0), never a hardcoded "upsize".
    monkeypatch.setattr(
        mod, "price_rds_instance_hour",
        lambda region, engine, cls, **k: 0.50 if cls == "db.r6g.large" else 0.10,
    )
    monkeypatch.setattr(mod, "price_rds_storage_month",
                        lambda *a, **k: {"storage_usd": 5.0, "iops_usd": 0.0})

    result = mod._simulate_rds_instance_rightsizing(
        "rds-mysql-1", new_instance_class="db.r6g.xlarge")

    assert result["recommendation"]["instance_class"] == "db.r6g.xlarge"
    assert result["cost_impact"]["delta_monthly_usd"] < 0
    assert result["recommendation"]["action"] == "downsize"


# ---------------------------------------------------------------------------
# Dispatcher — /rds-instance-rightsizing threads body fields
# ---------------------------------------------------------------------------

def test_dispatcher_rds_rightsizing(mod, monkeypatch):
    captured = {}

    def fake(cluster_id, window_hours=168, headroom=0.5, new_instance_class=None):
        captured["args"] = (cluster_id, window_hours, headroom, new_instance_class)
        return {"status": "ok"}

    monkeypatch.setattr(mod, "_simulate_rds_instance_rightsizing", fake)
    monkeypatch.setattr(mod.tenancy, "cluster_visible", lambda *a, **k: True)
    event = {
        "httpMethod": "POST",
        "rawPath": "/api/simulation/rds-instance-rightsizing",
        "body": '{"cluster_id": "rds-mysql-1", "window_hours": 336, '
                '"headroom": 0.7, "new_instance_class": "db.r6g.medium"}',
    }
    mod.lambda_handler(event, None)
    assert captured["args"] == ("rds-mysql-1", 336, 0.7, "db.r6g.medium")
