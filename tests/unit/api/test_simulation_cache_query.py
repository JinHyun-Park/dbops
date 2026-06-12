"""Regression test for the simulation REST handler's `_cache_query` helper.

A latent bug: `_cache_query` called rds-data `execute_statement` WITHOUT
`includeResultMetadata=True`, so the Data API omitted `columnMetadata` and the
column-name→value mapping produced EMPTY dict rows — every name-based `.get()`
returned None. The Aurora REST tools have live-describe fallbacks that masked it;
the DynamoDB capacity-cost tool (cache-only) surfaced it as a permanent no_data
(region="", datapoints=0) even though the cache had the rows.

These tests exercise the REAL `_cache_query` against a mocked rds-data client so a
future edit can't silently drop the flag again.
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
def mod(monkeypatch):
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:cache")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:secret")
    monkeypatch.setenv("CACHE_DB_NAME", "dbops")
    return _load()


def _fake_rds(records, columns):
    client = MagicMock()
    client.execute_statement.return_value = {
        "columnMetadata": [{"name": c} for c in columns],
        "records": records,
    }
    return client


def test_cache_query_passes_include_result_metadata(mod, monkeypatch):
    """The flag must be set, or columnMetadata is omitted and rows come back empty."""
    fake = _fake_rds(
        records=[[{"stringValue": "ap-northeast-2"}, {"stringValue": "PROVISIONED"}]],
        columns=["region", "billing_mode"],
    )
    monkeypatch.setattr(mod, "_rds_data", lambda: fake)

    mod._cache_query(
        "SELECT region, billing_mode FROM cluster_meta WHERE cluster_id = :cid",
        {"cid": "ddb-0d089ec02d21"},
    )

    _, kwargs = fake.execute_statement.call_args
    assert kwargs.get("includeResultMetadata") is True


def test_cache_query_maps_column_names_to_values(mod, monkeypatch):
    """With metadata present, rows must be name-keyed dicts (not empty)."""
    fake = _fake_rds(
        records=[[{"stringValue": "ap-northeast-2"}, {"stringValue": "PROVISIONED"}]],
        columns=["region", "billing_mode"],
    )
    monkeypatch.setattr(mod, "_rds_data", lambda: fake)

    rows = mod._cache_query("SELECT region, billing_mode FROM cluster_meta", {})

    assert rows == [{"region": "ap-northeast-2", "billing_mode": "PROVISIONED"}]
    assert rows[0].get("region") == "ap-northeast-2"


def test_cache_query_handles_null_and_numeric(mod, monkeypatch):
    fake = _fake_rds(
        records=[[{"longValue": 42}, {"isNull": True}]],
        columns=["datapoints", "billing_mode"],
    )
    monkeypatch.setattr(mod, "_rds_data", lambda: fake)

    rows = mod._cache_query("SELECT 1", {})

    assert rows[0]["datapoints"] == 42
    assert rows[0]["billing_mode"] is None
