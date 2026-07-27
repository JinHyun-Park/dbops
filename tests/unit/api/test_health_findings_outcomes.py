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


# ---------------------------------------------------------------------------
# Multi-writer families (rds_instance, documentdb): both Lambdas' findings
# surface via latest-per-check_type within a freshness window (Postgres does the
# row filtering; these tests pin the SQL contract + row pass-through, since the
# mock does not run SQL).
# ---------------------------------------------------------------------------

def _capture_findings_query(rows):
    """Query stub that records the cluster_health_findings SQL it was asked to
    run and returns the given rows for it. Outcome lookups return no history."""
    captured = {"sql": None}

    def query(sql, params=None):
        if "remediation_outcomes_agg" in sql:
            return [{"successes": 0, "attempts": 0}]
        if "FROM cluster_health_findings" in sql:
            captured["sql"] = sql
            return rows
        return []

    return query, captured


def test_rds_instance_two_snapshots_both_surface():
    """(a) rds_instance: findings from two different snapshot_times (the ETL
    Lambda and the direct-TCP Lambda) BOTH pass through, and the SQL selects
    the latest snapshot per check_type within a freshness window."""
    etl = _make_finding("capacity_forecast", "warning")
    etl["snapshot_time"] = "2026-07-24T10:00:00Z"
    innodb = _make_finding("innodb_history_list_high", "critical")
    innodb["id"] = 2
    innodb["snapshot_time"] = "2026-07-24T10:02:00Z"
    stub, captured = _capture_findings_query([etl, innodb])
    with patch(f"{_MODULE_NAME}._registry_engine", return_value="sqlserver-se"):
        out = handler._health_findings(stub, "rds1")
    check_types = {f["check_type"] for f in out["findings"]}
    assert check_types == {"capacity_forecast", "innodb_history_list_high"}, \
        "both Lambdas' findings must surface"
    _assert_multi_writer_sql(captured["sql"])
    # The panel's "as of" must be the freshest of the two writers' snapshots.
    assert out["snapshot_time"] == "2026-07-24T10:02:00Z"


def _assert_multi_writer_sql(sql):
    """SQL contract for a family with two writer Lambdas."""
    assert "PARTITION BY check_type" in sql, "latest snapshot must be per check_type"
    assert "INTERVAL '15 minutes'" in sql, "window must cover the slowest writer's cadence"
    # Must NOT collapse to a single global MAX snapshot (that hid one Lambda).
    assert "latest.ts" not in sql
    # Window is relative to the cluster's OWN newest finding, not wall clock, so
    # a single-snapshot cluster (seeded demo) keeps showing its findings and the
    # agent tool answers identically.
    assert "NOW()" not in sql
    assert "MAX(snapshot_time)" in sql


def test_documentdb_both_writers_surface():
    """(a2) documentdb has the same two-writer shape: etl_collector
    docdb_findings (5 CloudWatch check_types) + docdb_mongo_collector
    (docdb_mongo_long_running_ops), disjoint sets at different snapshot_times.
    A global MAX would have shown one side or the other, never both."""
    rows = []
    for i, ct in enumerate(("docdb_connection_saturation", "docdb_cost_oversized",
                            "docdb_cursor_timeout", "docdb_low_cache_hit",
                            "docdb_replica_lag")):
        r = _make_finding(ct, "warning")
        r["id"] = i + 1
        r["snapshot_time"] = "2026-07-24T10:00:00Z"
        rows.append(r)
    mongo = _make_finding("docdb_mongo_long_running_ops", "critical")
    mongo["id"] = 99
    mongo["snapshot_time"] = "2026-07-24T10:03:00Z"
    rows.append(mongo)

    stub, captured = _capture_findings_query(rows)
    with patch(f"{_MODULE_NAME}._registry_engine", return_value="docdb"):
        out = handler._health_findings(stub, "docdb1")

    assert {f["check_type"] for f in out["findings"]} == {
        "docdb_connection_saturation", "docdb_cost_oversized", "docdb_cursor_timeout",
        "docdb_low_cache_hit", "docdb_replica_lag", "docdb_mongo_long_running_ops",
    }, "both DocumentDB writers' findings must surface"
    _assert_multi_writer_sql(captured["sql"])


def test_multi_writer_window_survives_single_snapshot_cluster():
    """(b) the window bound subtracts from the cluster's own MAX(snapshot_time),
    so a cluster with exactly ONE findings snapshot (seeded demo, or collectors
    that just started) still has that snapshot inside the window. A NOW()-based
    bound would have blanked it."""
    stub, captured = _capture_findings_query([])
    with patch(f"{_MODULE_NAME}._registry_engine", return_value="mysql"):
        handler._health_findings(stub, "rds2")
    sql = " ".join(captured["sql"].split())
    assert ("snapshot_time >= ( SELECT MAX(snapshot_time) FROM cluster_health_findings "
            "WHERE cluster_id = :cid ) - INTERVAL '15 minutes'") in sql


def test_relational_uses_single_max_unchanged():
    """(c) relational (Aurora) behavior UNCHANGED: single global
    MAX(snapshot_time), no freshness window, no per-check_type partition."""
    stub, captured = _capture_findings_query([_make_finding()])
    with patch(f"{_MODULE_NAME}._registry_engine", return_value="aurora-postgresql"):
        handler._health_findings(stub, "aur1")
    sql = captured["sql"]
    assert "MAX(snapshot_time)" in sql and "latest.ts" in sql, \
        "relational must keep single-MAX snapshot query"
    assert "PARTITION BY check_type" not in sql
    assert "INTERVAL" not in sql
