import json
from unittest.mock import MagicMock

from mcp_servers.performance.tools.explain_plan import explain_plan_impl
from mcp_servers.shared.models import QueryResult


def _plan_result(plan_doc: dict) -> QueryResult:
    """Wrap a PG plan document the way rds-data surfaces EXPLAIN ... FORMAT JSON:
    a single row, single column named "QUERY PLAN", value is the JSON string of a
    one-element list."""
    return QueryResult(
        columns=["QUERY PLAN"],
        rows=[{"QUERY PLAN": json.dumps([plan_doc])}],
        row_count=1,
    )


def test_explain_plan_only_flags_seq_scan_on_large_table():
    # analyze=False: planner output only, no Actual Rows / Execution Time.
    plan_doc = {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "orders",
            "Plan Rows": 50000,
            "Total Cost": 1234.5,
        },
        "Planning Time": 0.42,
    }
    mock_cache = MagicMock()
    mock_cache.execute_on_target.return_value = _plan_result(plan_doc)

    result = explain_plan_impl(mock_cache, cluster_id="prod-pg-1", sql="SELECT * FROM orders")

    assert result["status"] == "ok"
    assert result["analyzed"] is False
    # Did NOT execute: no ANALYZE in the EXPLAIN we sent.
    explain_sql = mock_cache.execute_on_target.call_args.args[1]
    assert "ANALYZE" not in explain_sql.upper()
    assert "FORMAT JSON" in explain_sql.upper()

    seq = [f for f in result["findings"] if f["issue"] == "Sequential scan on large table"]
    assert len(seq) == 1
    assert seq[0]["severity"] == "high"
    assert seq[0]["relation"] == "orders"
    assert result["summary"]["planning_time_ms"] == 0.42
    assert "execution_time_ms" not in result["summary"]


def test_explain_plan_analyze_flags_row_estimate_miss_and_surfaces_timing():
    # analyze=True: estimate (100) vs actual (50000) is a 500x miss → medium finding.
    plan_doc = {
        "Plan": {
            "Node Type": "Index Scan",
            "Relation Name": "events",
            "Plan Rows": 100,
            "Actual Rows": 50000,
            "Total Cost": 980.0,
        },
        "Planning Time": 0.3,
        "Execution Time": 1234.56,
    }
    mock_cache = MagicMock()
    mock_cache.execute_on_target.return_value = _plan_result(plan_doc)

    result = explain_plan_impl(mock_cache, cluster_id="prod-pg-1", sql="SELECT * FROM events", analyze=True)

    assert result["status"] == "ok"
    assert result["analyzed"] is True
    explain_sql = mock_cache.execute_on_target.call_args.args[1]
    assert "ANALYZE" in explain_sql.upper()

    miss = [f for f in result["findings"] if f["issue"].startswith("Planner row estimate off")]
    assert len(miss) == 1
    assert miss[0]["severity"] == "medium"
    assert "500x" in miss[0]["issue"]
    assert result["summary"]["execution_time_ms"] == 1234.56
    assert result["summary"]["actual_rows"] == 50000
    assert result["expensive_nodes"][0]["actual_rows"] == 50000


def test_explain_plan_rejects_non_select():
    mock_cache = MagicMock()
    result = explain_plan_impl(mock_cache, cluster_id="prod-pg-1", sql="DELETE FROM orders")
    assert result["status"] == "rejected"
    # Never touched the target.
    mock_cache.execute_on_target.assert_not_called()


def test_explain_plan_unregistered_cluster_returns_no_target():
    mock_cache = MagicMock()
    mock_cache.execute_on_target.return_value = QueryResult(columns=[], rows=[], row_count=0)
    result = explain_plan_impl(mock_cache, cluster_id="unknown", sql="SELECT 1")
    assert result["status"] == "no_target"
    assert "register" in result["reason"]


def test_explain_plan_unparseable_plan_returns_error():
    mock_cache = MagicMock()
    mock_cache.execute_on_target.return_value = QueryResult(
        columns=["QUERY PLAN"],
        rows=[{"QUERY PLAN": "not-json-at-all (cost=0.00..1.00 rows=1)"}],
        row_count=1,
    )
    result = explain_plan_impl(mock_cache, cluster_id="prod-mysql-1", sql="SELECT 1")
    assert result["status"] == "error"
    assert "unsupported engine" in result["reason"]
    assert "raw_head" in result


# ===== analyze=True must not execute side-effecting / data-modifying SQL =====


def test_analyze_rejects_data_modifying_cte():
    """A data-modifying CTE passes the SELECT/WITH shape check but EXPLAIN
    ANALYZE would RUN the DELETE — analyze=True must reject it."""
    cache = MagicMock()
    out = explain_plan_impl(
        cache,
        cluster_id="prod-pg-1",
        sql="WITH d AS (DELETE FROM events WHERE id < 100 RETURNING *) SELECT count(*) FROM d",
        analyze=True,
    )
    assert out["status"] == "rejected"
    cache.execute_on_target.assert_not_called()


def test_analyze_rejects_side_effecting_function():
    cache = MagicMock()
    out = explain_plan_impl(
        cache, cluster_id="prod-pg-1",
        sql="SELECT pg_terminate_backend(123)", analyze=True,
    )
    assert out["status"] == "rejected"
    cache.execute_on_target.assert_not_called()


def test_plan_only_allows_cte_without_executing(monkeypatch):
    """analyze=False is plan-only (EXPLAIN never executes), so even a
    data-modifying CTE is safe to plan — it must NOT be rejected."""
    from mcp_servers.shared.models import QueryResult

    plan = [{"Plan": {"Node Type": "Aggregate", "Total Cost": 5.0, "Plan Rows": 1}}]
    cache = MagicMock()
    cache.execute_on_target.return_value = QueryResult(
        columns=["QUERY PLAN"], rows=[{"QUERY PLAN": __import__("json").dumps(plan)}], row_count=1
    )
    out = explain_plan_impl(
        cache, cluster_id="prod-pg-1",
        sql="WITH d AS (DELETE FROM t RETURNING *) SELECT count(*) FROM d",
        analyze=False,
    )
    assert out["status"] == "ok"
    # plan-only EXPLAIN, no ANALYZE
    sent = cache.execute_on_target.call_args.args[1]
    assert "ANALYZE" not in sent.upper()


def test_strip_explain_prefix_handles_bare_and_paren_forms():
    from mcp_servers.performance.tools.explain_plan import _strip_explain_prefix

    assert _strip_explain_prefix("EXPLAIN SELECT 1").strip() == "SELECT 1"
    assert _strip_explain_prefix("EXPLAIN ANALYZE VERBOSE SELECT 1").strip() == "SELECT 1"
    assert _strip_explain_prefix("EXPLAIN (ANALYZE, BUFFERS) SELECT 1").strip() == "SELECT 1"
    assert _strip_explain_prefix("SELECT 1").strip() == "SELECT 1"  # no prefix → unchanged
