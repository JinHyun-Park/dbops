"""Unit tests for the cost-handler ElastiCache path (?view=elasticache).

Mirrors test_cost_rds.py: same mocking approach (importlib load, MagicMock CE
client routed by GroupBy shape), same assertion style. Pins:
  - _elasticache_services SERVICE filtering and fallback behaviour
  - _handle_elasticache_view envelope keys + per_cluster_available flag
  - lambda_handler routing of ?view=elasticache

No real AWS calls.
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
    spec = importlib.util.spec_from_file_location("cost_handler_ec", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cost_handler_ec"] = module
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
# SERVICE discovery — keeps only ElastiCache-looking names, else falls back
# ---------------------------------------------------------------------------


def test_elasticache_services_filters_to_elasticache(mod):
    ce = MagicMock()
    ce.get_dimension_values.return_value = _dim_resp(
        [
            "Amazon ElastiCache",
            "Amazon Relational Database Service",
            "Amazon Bedrock",
            "Amazon EC2",
        ]
    )
    out = mod._elasticache_services(ce, START, END)
    assert "Amazon ElastiCache" in out
    assert "Amazon Relational Database Service" not in out
    assert "Amazon Bedrock" not in out
    assert "Amazon EC2" not in out


def test_elasticache_services_falls_back_when_discovery_empty(mod):
    ce = MagicMock()
    ce.get_dimension_values.return_value = _dim_resp(["Amazon EC2"])
    out = mod._elasticache_services(ce, START, END)
    assert out == list(mod._ELASTICACHE_SERVICE_DEFAULT)


def test_elasticache_services_falls_back_on_exception(mod):
    ce = MagicMock()
    ce.get_dimension_values.side_effect = RuntimeError("boom")
    out = mod._elasticache_services(ce, START, END)
    assert out == list(mod._ELASTICACHE_SERVICE_DEFAULT)


# ---------------------------------------------------------------------------
# Full ElastiCache view envelope — shape + per_cluster_available flag + note
# ---------------------------------------------------------------------------


def _ce_for_view(per_cluster_groups=None, per_cluster_raises=False):
    """Build a CE mock whose get_cost_and_usage routes by GroupBy:
    - no GroupBy        → daily total
    - DIMENSION groupby → usage-type breakdown
    - TAG groupby       → per-cluster (or raises / empty)."""
    ce = MagicMock()
    ce.get_dimension_values.return_value = _dim_resp(["Amazon ElastiCache"])

    def route(**kwargs):
        group_by = kwargs.get("GroupBy")
        if not group_by:
            return _total_resp([8.0, 9.0, 10.0])
        kind = group_by[0]["Type"]
        if kind == "DIMENSION":
            return _grouped_resp([[("USE1-NodeUsage:cache.r6g.large", 27.0, 720.0)]])
        # TAG
        if per_cluster_raises:
            raise RuntimeError("not currently activated")
        return _grouped_resp(per_cluster_groups or [[]])

    ce.get_cost_and_usage.side_effect = route
    return ce


def _body(resp):
    return json.loads(resp["body"])


def test_handle_elasticache_view_returns_correct_envelope(mod):
    ce = _ce_for_view(per_cluster_groups=[[("dbops:cluster$cache-prod-1", 27.0, 0.0)]])
    body = _body(mod._handle_elasticache_view(ce, START, END, 30))
    assert body["view"] == "elasticache"
    assert body["currency"] == "USD"
    assert body["total"] == pytest.approx(27.0)
    assert len(body["daily"]) == 3
    assert body["by_usage_type"][0]["usage_type"] == "USE1-NodeUsage:cache.r6g.large"
    assert body["per_cluster_available"] is True
    assert body["per_cluster_tag"] == "dbops:cluster"
    assert body["per_cluster"][0]["cluster"] == "cache-prod-1"
    assert body["per_cluster_note"] is None
    assert body["anomalies"] == []  # too few days for anomaly detection


def test_handle_elasticache_view_without_per_cluster_sets_flag_and_note(mod):
    ce = _ce_for_view(per_cluster_raises=True)
    body = _body(mod._handle_elasticache_view(ce, START, END, 30))
    assert body["per_cluster_available"] is False
    assert body["per_cluster"] == []
    assert body["per_cluster_tag"] is None
    assert body["per_cluster_note"]  # non-empty activation guidance
    assert "cost-allocation" in body["per_cluster_note"]
    # total + usage-type breakdown still present
    assert body["total"] == pytest.approx(27.0)
    assert len(body["by_usage_type"]) == 1


def test_lambda_handler_routes_view_elasticache(mod):
    """The ?view=elasticache query param must dispatch to the ElastiCache path."""
    ce = _ce_for_view(per_cluster_groups=[[]])
    mod.boto3 = MagicMock()
    mod.boto3.client.return_value = ce
    event = {"httpMethod": "GET", "queryStringParameters": {"view": "elasticache", "days": "30"}}
    body = _body(mod.lambda_handler(event, None))
    assert body["view"] == "elasticache"
