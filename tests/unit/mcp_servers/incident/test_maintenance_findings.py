"""get_maintenance_findings: family-aware findings surfacing + fail-closed.

Two families write cluster_health_findings from TWO Lambdas on independent
schedules with disjoint check_type sets (rds_instance, documentdb), so a single
global MAX(snapshot_time) shows only whichever ran last. These tests pin the
per-family SQL contract (Postgres does the row filtering, the mock does not run
SQL) plus the row pass-through, and pin that an unresolvable cluster returns an
explicit error instead of an empty list the agent would read as "all healthy".
"""

from unittest.mock import MagicMock

from mcp_servers.incident.tools.maintenance_findings import get_maintenance_findings_impl
from mcp_servers.shared.models import QueryResult


def _make_row(check_type, severity, subject="s", value_str="v",
              threshold_str="t", recommendation="r", snapshot_time="2026-07-24T10:00:00Z"):
    return {
        "check_type": check_type,
        "severity": severity,
        "subject": subject,
        "value_str": value_str,
        "threshold_str": threshold_str,
        "recommendation": recommendation,
        "details": "{}",
        "snapshot_time": snapshot_time,
    }


def _cache(engine, findings_rows, findings_raises=False):
    """Cache stub: answers the cluster_meta engine lookup, then the findings
    query. `engine=None` simulates a cluster with no cluster_meta row."""
    captured = {"sql": None}

    def execute(sql, params=None):
        if "cluster_meta" in sql:
            rows = [{"engine": engine}] if engine else []
            return QueryResult(columns=["engine"], rows=rows, row_count=len(rows))
        captured["sql"] = sql
        if findings_raises:
            raise RuntimeError(
                "BadRequestException: relation cluster_health_findings does not exist; "
                "secret arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:x"
            )
        return QueryResult(
            columns=list(findings_rows[0].keys()) if findings_rows else [],
            rows=findings_rows,
            row_count=len(findings_rows),
        )

    cache = MagicMock()
    cache.execute.side_effect = execute
    return cache, captured


# ---------------------------------------------------------------------------
# Single-writer families: behavior must NOT move
# ---------------------------------------------------------------------------

def test_relational_keeps_single_max_snapshot():
    """Aurora has ONE findings writer (the ETL cycle's shared run_ts), so it
    keeps the global MAX(snapshot_time) query: no window, no partition."""
    cache, captured = _cache("aurora-postgresql", [_make_row("pg_vacuum_due", "info")])
    out = get_maintenance_findings_impl(cache, cluster_id="aurora-pg-prod")

    assert out["status"] == "ok"
    assert out["engine_family"] == "relational"
    assert out["counts"] == {"critical": 0, "warning": 0, "info": 1}
    assert out["findings"][0]["check_type"] == "pg_vacuum_due"

    sql = captured["sql"]
    assert "MAX(snapshot_time)" in sql and "cluster_health_findings" in sql
    assert "PARTITION BY check_type" not in sql
    assert "INTERVAL" not in sql


def test_seeded_single_snapshot_cluster_still_returns_findings():
    """The seeded demo cluster (api/clusters/seeder.py) writes ONE findings
    snapshot at seed time and never re-emits it. It must still surface."""
    seeded = [
        _make_row("cost_serverless_max_too_high", "warning", snapshot_time="2026-01-01T00:00:00Z"),
        _make_row("pg_unused_index", "info", snapshot_time="2026-01-01T00:00:00Z"),
    ]
    cache, _ = _cache("aurora-postgresql", seeded)
    out = get_maintenance_findings_impl(cache, cluster_id="sample-cluster")

    assert len(out["findings"]) == 2, "a single-snapshot cluster must not look healthy"


def test_dynamodb_stays_on_single_max():
    """dynamodb findings come from the ETL collector only: single writer."""
    cache, captured = _cache("dynamodb", [_make_row("ddb_throttling", "critical")])
    out = get_maintenance_findings_impl(cache, cluster_id="ddb-abc123")

    assert out["engine_family"] == "dynamodb"
    assert out["counts"]["critical"] == 1
    assert "PARTITION BY check_type" not in captured["sql"]


# ---------------------------------------------------------------------------
# Multi-writer families: both writers' findings must surface
# ---------------------------------------------------------------------------

def _assert_multi_writer_sql(sql):
    assert "PARTITION BY check_type" in sql, "latest snapshot must be per check_type"
    assert "INTERVAL '15 minutes'" in sql, "window must cover the slowest writer's cadence"
    # Window is measured from the cluster's own newest finding, NOT wall clock:
    # a NOW()-relative window blanks the seeded single-snapshot cluster.
    assert "NOW()" not in sql
    assert "MAX(snapshot_time)" in sql


def test_documentdb_both_writers_surface():
    """documentdb: etl_collector docdb_findings + docdb_mongo_collector write at
    DIFFERENT snapshot_times with disjoint check_types. All must come back."""
    cw = [
        _make_row("docdb_connection_saturation", "warning", snapshot_time="2026-07-24T10:00:00Z"),
        _make_row("docdb_low_cache_hit", "warning", snapshot_time="2026-07-24T10:00:00Z"),
        _make_row("docdb_replica_lag", "critical", snapshot_time="2026-07-24T10:00:00Z"),
        _make_row("docdb_cursor_timeout", "info", snapshot_time="2026-07-24T10:00:00Z"),
        _make_row("docdb_cost_oversized", "info", snapshot_time="2026-07-24T10:00:00Z"),
    ]
    mongo = [
        _make_row("docdb_mongo_long_running_ops", "critical", snapshot_time="2026-07-24T10:03:00Z"),
    ]
    cache, captured = _cache("docdb", cw + mongo)
    out = get_maintenance_findings_impl(cache, cluster_id="docdb-1")

    assert out["engine_family"] == "documentdb"
    assert {f["check_type"] for f in out["findings"]} == {
        "docdb_connection_saturation", "docdb_low_cache_hit", "docdb_replica_lag",
        "docdb_cursor_timeout", "docdb_cost_oversized", "docdb_mongo_long_running_ops",
    }, "both DocumentDB writers' findings must surface"
    assert out["counts"] == {"critical": 2, "warning": 2, "info": 2}
    _assert_multi_writer_sql(captured["sql"])


def test_rds_instance_both_writers_surface():
    """rds_instance: etl_collector + rds_direct_collector (InnoDB status)."""
    etl = [_make_row("capacity_forecast", "warning", snapshot_time="2026-07-24T10:00:00Z")]
    direct = [_make_row("innodb_history_list_high", "critical",
                        snapshot_time="2026-07-24T10:02:00Z")]
    cache, captured = _cache("mysql", etl + direct)
    out = get_maintenance_findings_impl(cache, cluster_id="rds-mysql-1")

    assert out["engine_family"] == "rds_instance"
    assert {f["check_type"] for f in out["findings"]} == {
        "capacity_forecast", "innodb_history_list_high",
    }
    _assert_multi_writer_sql(captured["sql"])


def test_sqlserver_uses_multi_writer_path():
    """SQL Server is the other rds_instance engine: same two writers."""
    cache, captured = _cache("sqlserver-se", [_make_row("cost_idle_instance", "info")])
    out = get_maintenance_findings_impl(cache, cluster_id="rds-mssql-1")
    assert out["engine_family"] == "rds_instance"
    _assert_multi_writer_sql(captured["sql"])


# ---------------------------------------------------------------------------
# FAIL-CLOSED: an empty list would read as "no maintenance issues"
# ---------------------------------------------------------------------------

def test_unresolvable_cluster_returns_error_not_empty_findings():
    """No cluster_meta row → explicit error status, and NO findings key at all,
    so the agent cannot report the cluster as clean."""
    cache, captured = _cache(None, [])
    out = get_maintenance_findings_impl(cache, cluster_id="ghost-cluster")

    assert out["status"] == "error"
    assert "findings" not in out
    assert out["reason"]
    assert captured["sql"] is None, "must not query findings for an unknown cluster"


def test_missing_cluster_id_returns_error():
    cache, _ = _cache("aurora-postgresql", [])
    out = get_maintenance_findings_impl(cache, cluster_id="")
    assert out["status"] == "error"
    assert "findings" not in out


def test_family_lookup_failure_returns_error_without_leaking():
    cache = MagicMock()
    cache.execute.side_effect = RuntimeError(
        "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:dbops-cache-AbCdEf"
    )
    out = get_maintenance_findings_impl(cache, cluster_id="c1")

    assert out["status"] == "error"
    assert "findings" not in out
    assert "secretsmanager" not in out["reason"]


def test_findings_query_failure_returns_error_without_leaking():
    cache, _ = _cache("docdb", [], findings_raises=True)
    out = get_maintenance_findings_impl(cache, cluster_id="docdb-2")

    assert out["status"] == "error"
    assert "findings" not in out
    assert "secretsmanager" not in out["reason"]
    assert "cluster_health_findings" not in out["reason"]


def test_resolved_cluster_with_no_findings_is_ok_and_empty():
    """A genuinely healthy cluster: resolved family, zero rows → ok + empty."""
    cache, _ = _cache("aurora-mysql", [])
    out = get_maintenance_findings_impl(cache, cluster_id="healthy-1")

    assert out["status"] == "ok"
    assert out["findings"] == []
    assert out["counts"] == {"critical": 0, "warning": 0, "info": 0}
