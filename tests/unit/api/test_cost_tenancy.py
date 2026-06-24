"""Unit tests for multi-team tenancy enforcement on the cost handler.

Tests that per_cluster rows are filtered to the caller's visible cluster set
for ?view=rds and ?view=elasticache.  Admin (visible=None) => unfiltered.

No real AWS calls: _query_per_cluster is patched to return deterministic rows;
tenancy.visible_set_from_registry is patched to control visibility.
"""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
HANDLER_PATH = ROOT / "api" / "cost" / "handler.py"
TENANCY_PATH = ROOT / "api" / "cost" / "tenancy.py"


def _load():
    spec = importlib.util.spec_from_file_location("cost_handler_tenancy", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cost_handler_tenancy"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def mod():
    return _load()


START = date(2026, 5, 1)
END = date(2026, 5, 31)


def _body(resp):
    return json.loads(resp["body"])


def _minimal_ce(per_cluster_rows):
    """CE mock that returns minimal data; per_cluster_rows for TAG groupby."""
    ce = MagicMock()
    ce.get_dimension_values.return_value = {
        "DimensionValues": [{"Value": "Amazon Relational Database Service"}]
    }

    def route(**kwargs):
        group_by = kwargs.get("GroupBy")
        if not group_by:
            return {
                "ResultsByTime": [
                    {"TimePeriod": {"Start": "2026-05-01"}, "Total": {"UnblendedCost": {"Amount": "10.0"}}}
                ]
            }
        kind = group_by[0]["Type"]
        if kind == "DIMENSION":
            return {"ResultsByTime": []}
        # TAG groupby
        groups = [
            {"Keys": [f"dbops:cluster${r['cluster']}"], "Metrics": {"UnblendedCost": {"Amount": str(r["amount"])}}}
            for r in per_cluster_rows
        ]
        return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-05-01"}, "Groups": groups}]}

    ce.get_cost_and_usage.side_effect = route
    return ce


# Three clusters: c-open (unassigned), c-teamA (team A), c-teamB (team B)
_ALL_ROWS = [
    {"cluster": "c-open", "amount": 5.0},
    {"cluster": "c-teamA", "amount": 3.0},
    {"cluster": "c-teamB", "amount": 2.0},
]

_VIEWER_EVENT = {
    "httpMethod": "GET",
    "queryStringParameters": {"view": "rds", "days": "30"},
    "headers": {"authorization": "Bearer viewer-token"},
}

_ADMIN_EVENT = {
    "httpMethod": "GET",
    "queryStringParameters": {"view": "rds", "days": "30"},
    "headers": {"authorization": "Bearer admin-token"},
}


# ---------------------------------------------------------------------------
# ?view=rds  — viewer sees only c-open + c-teamA
# ---------------------------------------------------------------------------


def test_rds_view_filters_per_cluster_for_viewer(mod, monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    ce = _minimal_ce(_ALL_ROWS)
    monkeypatch.setattr(mod, "_query_per_cluster", lambda *a: (_ALL_ROWS, "dbops:cluster", None))
    monkeypatch.setattr(mod.tenancy, "visible_set_from_registry", lambda event: {"c-open", "c-teamA"})

    resp = mod._handle_rds_view(ce, START, END, 30, _VIEWER_EVENT)
    body = _body(resp)
    clusters = {r["cluster"] for r in body["per_cluster"]}
    assert "c-teamB" not in clusters
    assert "c-open" in clusters
    assert "c-teamA" in clusters
    assert body["per_cluster_available"] is True


def test_rds_view_admin_sees_all_clusters(mod, monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    ce = _minimal_ce(_ALL_ROWS)
    monkeypatch.setattr(mod, "_query_per_cluster", lambda *a: (_ALL_ROWS, "dbops:cluster", None))
    monkeypatch.setattr(mod.tenancy, "visible_set_from_registry", lambda event: None)

    resp = mod._handle_rds_view(ce, START, END, 30, _ADMIN_EVENT)
    body = _body(resp)
    clusters = {r["cluster"] for r in body["per_cluster"]}
    assert clusters == {"c-open", "c-teamA", "c-teamB"}


def test_rds_view_per_cluster_available_false_when_all_filtered(mod, monkeypatch):
    """If all per_cluster rows are filtered out, per_cluster_available becomes False."""
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    ce = _minimal_ce(_ALL_ROWS)
    monkeypatch.setattr(mod, "_query_per_cluster", lambda *a: (_ALL_ROWS, "dbops:cluster", None))
    # viewer can only see c-other which has no rows
    monkeypatch.setattr(mod.tenancy, "visible_set_from_registry", lambda event: {"c-other"})

    resp = mod._handle_rds_view(ce, START, END, 30, _VIEWER_EVENT)
    body = _body(resp)
    assert body["per_cluster"] == []
    assert body["per_cluster_available"] is False


# ---------------------------------------------------------------------------
# ?view=elasticache  — same filter logic
# ---------------------------------------------------------------------------


def test_elasticache_view_filters_per_cluster_for_viewer(mod, monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    ec_ce = MagicMock()
    ec_ce.get_dimension_values.return_value = {"DimensionValues": [{"Value": "Amazon ElastiCache"}]}

    def ec_route(**kwargs):
        group_by = kwargs.get("GroupBy")
        if not group_by:
            return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-05-01"}, "Total": {"UnblendedCost": {"Amount": "5.0"}}}]}
        kind = group_by[0]["Type"]
        if kind == "DIMENSION":
            return {"ResultsByTime": []}
        groups = [
            {"Keys": [f"dbops:cluster${r['cluster']}"], "Metrics": {"UnblendedCost": {"Amount": str(r["amount"])}}}
            for r in _ALL_ROWS
        ]
        return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-05-01"}, "Groups": groups}]}

    ec_ce.get_cost_and_usage.side_effect = ec_route
    monkeypatch.setattr(mod, "_query_per_cluster", lambda *a: (_ALL_ROWS, "dbops:cluster", None))
    monkeypatch.setattr(mod.tenancy, "visible_set_from_registry", lambda event: {"c-open", "c-teamA"})

    ec_event = {**_VIEWER_EVENT, "queryStringParameters": {"view": "elasticache", "days": "30"}}
    resp = mod._handle_elasticache_view(ec_ce, START, END, 30, ec_event)
    body = _body(resp)
    clusters = {r["cluster"] for r in body["per_cluster"]}
    assert "c-teamB" not in clusters
    assert {"c-open", "c-teamA"} <= clusters


def test_elasticache_view_admin_sees_all_clusters(mod, monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    ec_ce = MagicMock()
    ec_ce.get_dimension_values.return_value = {"DimensionValues": [{"Value": "Amazon ElastiCache"}]}
    monkeypatch.setattr(mod, "_query_per_cluster", lambda *a: (_ALL_ROWS, "dbops:cluster", None))
    monkeypatch.setattr(mod.tenancy, "visible_set_from_registry", lambda event: None)

    ec_event = {**_ADMIN_EVENT, "queryStringParameters": {"view": "elasticache", "days": "30"}}
    resp = mod._handle_elasticache_view(ec_ce, START, END, 30, ec_event)
    body = _body(resp)
    clusters = {r["cluster"] for r in body["per_cluster"]}
    assert clusters == {"c-open", "c-teamA", "c-teamB"}


# ---------------------------------------------------------------------------
# lambda_handler routing threads event correctly
# ---------------------------------------------------------------------------


def test_lambda_handler_rds_threads_event(mod, monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    ce = _minimal_ce(_ALL_ROWS)
    monkeypatch.setattr(mod, "boto3", MagicMock(**{"client.return_value": ce}))
    monkeypatch.setattr(mod, "_query_per_cluster", lambda *a: (_ALL_ROWS, "dbops:cluster", None))
    monkeypatch.setattr(mod.tenancy, "visible_set_from_registry", lambda event: {"c-open", "c-teamA"})

    body = _body(mod.lambda_handler(_VIEWER_EVENT, None))
    assert body["view"] == "rds"
    clusters = {r["cluster"] for r in body["per_cluster"]}
    assert "c-teamB" not in clusters
