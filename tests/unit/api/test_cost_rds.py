"""Unit tests for the cost-handler RDS/Aurora path (?view=rds).

These pin the parsing of mocked Cost Explorer responses: SERVICE discovery,
the USAGE_TYPE breakdown rollup, and — most importantly — the tag-based
per-cluster attribution behaviour, which must NEVER fabricate per-cluster
rows. When no cost-allocation tag yields data, the handler returns
`per_cluster_available: false` with an activation note instead.

No real AWS calls: a MagicMock CE client routes each method by argument shape.
"""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
HANDLER_PATH = ROOT / "api" / "cost" / "handler.py"


def _load():
    spec = importlib.util.spec_from_file_location("cost_handler", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cost_handler"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def mod():
    return _load()


START = date(2026, 5, 1)
END = date(2026, 5, 31)


def _dim_resp(values):
    return {"DimensionValues": [{"Value": v} for v in values]}


def _total_resp(daily_amounts):
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": f"2026-05-{i + 1:02d}"},
                "Total": {"UnblendedCost": {"Amount": str(amt)}},
            }
            for i, amt in enumerate(daily_amounts)
        ]
    }


def _grouped_resp(groups_per_day):
    """groups_per_day: list of [(key, amount, qty), ...] per day."""
    out = []
    for i, groups in enumerate(groups_per_day):
        out.append(
            {
                "TimePeriod": {"Start": f"2026-05-{i + 1:02d}"},
                "Groups": [
                    {
                        "Keys": [key],
                        "Metrics": {
                            "UnblendedCost": {"Amount": str(amt)},
                            "UsageQuantity": {"Amount": str(qty)},
                        },
                    }
                    for key, amt, qty in groups
                ],
            }
        )
    return {"ResultsByTime": out}


# ---------------------------------------------------------------------------
# SERVICE discovery — keeps only RDS/Aurora-looking names, else falls back
# ---------------------------------------------------------------------------


def test_rds_services_filters_to_rds_family(mod):
    ce = MagicMock()
    ce.get_dimension_values.return_value = _dim_resp(
        [
            "Amazon Relational Database Service",
            "Amazon Aurora",
            "Amazon Bedrock",
            "Amazon EC2",
        ]
    )
    out = mod._rds_services(ce, START, END)
    assert "Amazon Relational Database Service" in out
    assert "Amazon Aurora" in out
    assert "Amazon Bedrock" not in out
    assert "Amazon EC2" not in out


def test_rds_services_falls_back_when_discovery_empty(mod):
    ce = MagicMock()
    ce.get_dimension_values.return_value = _dim_resp(["Amazon EC2"])
    out = mod._rds_services(ce, START, END)
    assert out == mod._RDS_SERVICE_DEFAULT


def test_rds_services_falls_back_on_exception(mod):
    ce = MagicMock()
    ce.get_dimension_values.side_effect = RuntimeError("boom")
    out = mod._rds_services(ce, START, END)
    assert out == mod._RDS_SERVICE_DEFAULT


# ---------------------------------------------------------------------------
# USAGE_TYPE breakdown — rolls up amount + quantity across days, sorted desc
# ---------------------------------------------------------------------------


def test_query_by_dimension_rolls_up_and_sorts(mod):
    ce = MagicMock()
    ce.get_cost_and_usage.return_value = _grouped_resp(
        [
            [("APN1-Aurora:StorageIOUsage", 1.0, 100.0), ("APN1-InstanceUsage:db.r6g", 5.0, 24.0)],
            [("APN1-Aurora:StorageIOUsage", 2.0, 200.0)],
        ]
    )
    rows, err = mod._query_by_dimension(ce, START, END, ["Amazon RDS"], "USAGE_TYPE")
    assert err is None
    # InstanceUsage (5.0) sorts above StorageIOUsage (3.0 total)
    assert rows[0]["usage_type"] == "APN1-InstanceUsage:db.r6g"
    assert rows[0]["amount"] == pytest.approx(5.0)
    assert rows[1]["usage_type"] == "APN1-Aurora:StorageIOUsage"
    assert rows[1]["amount"] == pytest.approx(3.0)
    assert rows[1]["quantity"] == pytest.approx(300.0)


def test_query_by_dimension_returns_error_string(mod):
    ce = MagicMock()
    ce.get_cost_and_usage.side_effect = RuntimeError("ce exploded with secrets")
    rows, err = mod._query_by_dimension(ce, START, END, ["Amazon RDS"], "USAGE_TYPE")
    assert rows == []
    assert err is not None
    assert len(err) <= 200


# ---------------------------------------------------------------------------
# Per-cluster attribution — tag-based, never fabricated
# ---------------------------------------------------------------------------


def test_per_cluster_parses_tag_value_and_skips_untagged(mod):
    ce = MagicMock()
    ce.get_cost_and_usage.return_value = _grouped_resp(
        [
            [
                ("dbops:cluster$prod-aurora-1", 10.0, 0.0),
                ("dbops:cluster$prod-aurora-2", 4.0, 0.0),
                ("dbops:cluster$", 99.0, 0.0),  # untagged bucket — must be skipped
            ]
        ]
    )
    rows, tag_key, err = mod._query_per_cluster(ce, START, END, ["Amazon RDS"])
    assert err is None
    assert tag_key == "dbops:cluster"  # first candidate that yields data
    clusters = {r["cluster"]: r["amount"] for r in rows}
    assert clusters == {"prod-aurora-1": 10.0, "prod-aurora-2": 4.0}
    assert "" not in clusters  # untagged skipped
    # sorted desc
    assert rows[0]["cluster"] == "prod-aurora-1"


def test_per_cluster_not_activated_returns_flag(mod):
    ce = MagicMock()
    ce.get_cost_and_usage.side_effect = RuntimeError(
        "The tag dbops:cluster is not currently activated for cost allocation"
    )
    rows, tag_key, err = mod._query_per_cluster(ce, START, END, ["Amazon RDS"])
    assert rows == []
    assert tag_key is None
    assert err == "cost_allocation_tag_not_activated"


def test_per_cluster_empty_when_no_tag_yields_data(mod):
    """All candidate tags return empty group sets → no per-cluster rows,
    and crucially no fabricated numbers."""
    ce = MagicMock()
    ce.get_cost_and_usage.return_value = {"ResultsByTime": [{"Groups": []}]}
    rows, tag_key, err = mod._query_per_cluster(ce, START, END, ["Amazon RDS"])
    assert rows == []
    assert tag_key is None
    assert err is None


# ---------------------------------------------------------------------------
# Full RDS view envelope — shape + per_cluster_available flag + note
# ---------------------------------------------------------------------------


def _ce_for_view(per_cluster_groups=None, per_cluster_raises=False):
    """Build a CE mock whose get_cost_and_usage routes by GroupBy:
    - no GroupBy        → daily total
    - DIMENSION groupby → usage-type breakdown
    - TAG groupby       → per-cluster (or raises / empty)."""
    ce = MagicMock()
    ce.get_dimension_values.return_value = _dim_resp(["Amazon Relational Database Service"])

    def route(**kwargs):
        group_by = kwargs.get("GroupBy")
        if not group_by:
            return _total_resp([10.0, 12.0, 11.0])
        kind = group_by[0]["Type"]
        if kind == "DIMENSION":
            return _grouped_resp([[("APN1-Aurora:StorageIOUsage", 33.0, 1000.0)]])
        # TAG
        if per_cluster_raises:
            raise RuntimeError("not currently activated")
        return _grouped_resp(per_cluster_groups or [[]])

    ce.get_cost_and_usage.side_effect = route
    return ce


def _body(resp):
    return json.loads(resp["body"])


def test_handle_rds_view_with_per_cluster_data(mod):
    ce = _ce_for_view(
        per_cluster_groups=[[("dbops:cluster$prod-1", 20.0, 0.0)]]
    )
    body = _body(mod._handle_rds_view(ce, START, END, 30))
    assert body["view"] == "rds"
    assert body["currency"] == "USD"
    assert body["total"] == pytest.approx(33.0)
    assert len(body["daily"]) == 3
    assert body["by_usage_type"][0]["usage_type"] == "APN1-Aurora:StorageIOUsage"
    assert body["per_cluster_available"] is True
    assert body["per_cluster_tag"] == "dbops:cluster"
    assert body["per_cluster"][0]["cluster"] == "prod-1"
    assert body["per_cluster_note"] is None


def test_handle_rds_view_without_per_cluster_sets_flag_and_note(mod):
    ce = _ce_for_view(per_cluster_raises=True)
    body = _body(mod._handle_rds_view(ce, START, END, 30))
    assert body["per_cluster_available"] is False
    assert body["per_cluster"] == []
    assert body["per_cluster_tag"] is None
    assert body["per_cluster_note"]  # non-empty activation guidance
    assert "cost-allocation tag" in body["per_cluster_note"]
    # total/breakdown still present even when per-cluster is unavailable
    assert body["total"] == pytest.approx(33.0)
    assert len(body["by_usage_type"]) == 1


def test_lambda_handler_routes_view_rds(mod):
    """The ?view=rds query param must dispatch to the RDS path."""
    ce = _ce_for_view(per_cluster_groups=[[]])
    mod.boto3 = MagicMock()
    mod.boto3.client.return_value = ce
    event = {"httpMethod": "GET", "queryStringParameters": {"view": "rds", "days": "30"}}
    body = _body(mod.lambda_handler(event, None))
    assert body["view"] == "rds"


def test_lambda_handler_default_view_is_bedrock(mod):
    """No view param → Bedrock path (no 'view' key, has discovered_services)."""
    ce = MagicMock()
    ce.get_dimension_values.return_value = _dim_resp(["Amazon Bedrock"])
    ce.get_cost_and_usage.return_value = _total_resp([1.0, 2.0])
    mod.boto3 = MagicMock()
    mod.boto3.client.return_value = ce
    event = {"httpMethod": "GET", "queryStringParameters": None}
    body = _body(mod.lambda_handler(event, None))
    assert body.get("view") != "rds"
    assert "total_tagged" in body  # Bedrock-only field
