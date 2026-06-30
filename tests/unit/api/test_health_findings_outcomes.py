"""Tests: _health_findings enriches findings with outcome track record + re-ranks."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[3]
_HANDLER_PATH = _REPO / "api" / "dashboard" / "handler.py"
_HANDLER_DIR = _HANDLER_PATH.parent

# Insert handler dir so sibling imports (tenancy, engine_family) resolve.
sys.path.insert(0, str(_HANDLER_DIR))

_spec = importlib.util.spec_from_file_location("dashboard_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

# Register in sys.modules so unittest.mock.patch can resolve the target by name.
sys.modules["dashboard_handler"] = handler

# The module was registered as "dashboard_handler" — patch targets use that name.
_MODULE_NAME = "dashboard_handler"


def teardown_module(_):
    # Restore sys.path and sys.modules hygiene.
    if str(_HANDLER_DIR) in sys.path:
        sys.path.remove(str(_HANDLER_DIR))
    sys.modules.pop("dashboard_handler", None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_finding(check_type="query_regression", severity="warning"):
    return {
        "id": 1,
        "check_type": check_type,
        "severity": severity,
        "subject": "s",
        "value_str": "",
        "threshold_str": "",
        "recommendation": "인덱스",
        "details": "{}",
        "snapshot_time": "t",
    }


def _query_stub_with_outcome(successes, attempts):
    """Returns a query callable that yields one finding + one outcome row."""
    def query(sql, params=None):
        if "remediation_outcomes_agg" in sql:
            return [{"successes": successes, "attempts": attempts}]
        if "FROM cluster_health_findings" in sql:
            return [_make_finding()]
        return []

    return query


def _query_stub_no_cluster_history():
    """cluster row returns 0 attempts; fleet '*' row provides history."""
    cluster_queried = {"count": 0}

    def query(sql, params=None):
        if "remediation_outcomes_agg" in sql:
            # fleet query has cluster_id = '*' literal in SQL, no :cid param
            if "cluster_id = '*'" in sql:
                return [{"successes": 3, "attempts": 6}]
            cluster_queried["count"] += 1
            return [{"successes": 0, "attempts": 0}]
        if "FROM cluster_health_findings" in sql:
            return [_make_finding("slow_query", "critical")]
        return []

    return query, cluster_queried


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_finding_carries_outcome_track_record():
    """Finding gains outcome dict with cluster-level successes/attempts."""
    with patch(f"{_MODULE_NAME}._registry_engine", return_value="aurora-postgresql"):
        out = handler._health_findings(_query_stub_with_outcome(4, 5), "c1")
    f = out["findings"][0]
    assert f["outcome"] == {"successes": 4, "attempts": 5}


def test_finding_falls_back_to_fleet_when_no_cluster_history():
    """When cluster has 0 attempts, outcome comes from the '*' fleet row."""
    stub, _ = _query_stub_no_cluster_history()
    with patch(f"{_MODULE_NAME}._registry_engine", return_value="aurora-postgresql"):
        out = handler._health_findings(stub, "c2")
    f = out["findings"][0]
    assert f["outcome"] == {"successes": 3, "attempts": 6}


def test_findings_reranked_by_severity_then_success_rate():
    """Higher-severity findings sort before lower; within same severity,
    higher success_rate sorts first."""

    def query(sql, params=None):
        if "remediation_outcomes_agg" in sql:
            sc = (params or {}).get("sc", "")
            if "cluster_id = '*'" in sql:
                return [{"successes": 0, "attempts": 0}]
            if "finding:a" in sc:
                return [{"successes": 2, "attempts": 4}]
            return [{"successes": 1, "attempts": 2}]
        if "FROM cluster_health_findings" in sql:
            return [
                {"id": 1, "check_type": "a", "severity": "warning",
                 "subject": "s", "value_str": "", "threshold_str": "",
                 "recommendation": "r", "details": "{}", "snapshot_time": "t"},
                {"id": 2, "check_type": "b", "severity": "critical",
                 "subject": "s", "value_str": "", "threshold_str": "",
                 "recommendation": "r", "details": "{}", "snapshot_time": "t"},
            ]
        return []

    with patch(f"{_MODULE_NAME}._registry_engine", return_value="aurora-postgresql"):
        out = handler._health_findings(query, "c3")
    severities = [f["severity"] for f in out["findings"]]
    assert severities[0] == "critical", "critical must sort first"
