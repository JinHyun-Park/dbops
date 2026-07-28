"""E-2 (Aurora MySQL parity) for the performance server's engine-aware tools.

The trap this tier is built on: Aurora PG and Aurora MySQL are the SAME
capability family (relational), so `explain: True` / `index_advice: True` already
let Aurora MySQL through the handler gate. The dialect has to be resolved from
the ENGINE STRING inside the tool. These tests assert three things per tool:
Aurora MySQL gets a real, MySQL-shaped answer (or an explicit refusal naming the
engine), Aurora PG behaviour is unchanged, and an engine lookup that fails
degrades to the PG path rather than guessing.

The MySQL plan documents below are VERBATIM captures from `EXPLAIN FORMAT=JSON`
run over the Data API against the live Aurora MySQL 8.0.39 demo cluster
(dbops-dev-sample-samplemysql2c3d76ef-wsvoyba1dsfw, sampledb). Trimmed only of
`used_columns`. Hand-written fixtures are exactly how a cross-engine shape
mismatch slips through, so these are not hand-written.
"""

import json
from unittest.mock import MagicMock

import pytest
from mcp_servers.performance.tools.explain_plan import explain_plan_impl
from mcp_servers.performance.tools.recommend_index import recommend_index_impl
from mcp_servers.performance.tools.vacuum_stats import get_vacuum_stats_impl
from mcp_servers.shared.cache_client import is_mysql_engine
from mcp_servers.shared.models import QueryResult

# --- live captures -------------------------------------------------------

# SELECT s.id, p.name FROM sales s JOIN products p ON p.id = s.product_id
#   WHERE s.total_price > 100 ORDER BY s.sale_date LIMIT 20
_LIVE_JOIN_PLAN = {
    "query_block": {
        "select_id": 1,
        "cost_info": {"query_cost": "602995.88"},
        "ordering_operation": {
            "using_filesort": True,
            "nested_loop": [
                {"table": {
                    "table_name": "s",
                    "access_type": "ALL",
                    "rows_examined_per_scan": 1284750,
                    "rows_produced_per_join": 428207,
                    "filtered": "33.33",
                    "cost_info": {"read_cost": "89147.28", "eval_cost": "42820.72",
                                  "prefix_cost": "131968.00", "data_read_per_join": "9M"},
                    "attached_condition": "(`sampledb`.`s`.`total_price` > 100.00)",
                }},
                {"table": {
                    "table_name": "p",
                    "access_type": "eq_ref",
                    "possible_keys": ["PRIMARY"],
                    "key": "PRIMARY",
                    "used_key_parts": ["id"],
                    "key_length": "4",
                    "ref": ["sampledb.s.product_id"],
                    "rows_examined_per_scan": 1,
                    "rows_produced_per_join": 428207,
                    "filtered": "100.00",
                    "cost_info": {"read_cost": "428207.17", "eval_cost": "42820.72",
                                  "prefix_cost": "602995.88", "data_read_per_join": "254M"},
                }},
            ],
        },
    }
}

# SELECT product_id, COUNT(*) c FROM sales GROUP BY product_id ORDER BY c DESC
_LIVE_GROUP_PLAN = {
    "query_block": {
        "select_id": 1,
        "cost_info": {"query_cost": "131968.00"},
        "ordering_operation": {
            "using_filesort": True,
            "grouping_operation": {
                "using_temporary_table": True,
                "using_filesort": False,
                "table": {
                    "table_name": "sales",
                    "access_type": "ALL",
                    "rows_examined_per_scan": 1284750,
                    "rows_produced_per_join": 1284750,
                    "filtered": "100.00",
                    "cost_info": {"read_cost": "3493.00", "eval_cost": "128475.00",
                                  "prefix_cost": "131968.00", "data_read_per_join": "29M"},
                },
            },
        },
    }
}


# SELECT s.id, p.name FROM sales s LEFT JOIN products p ON p.id = 7
#   WHERE s.total_price > 999999
#
# Captured live for the prefix_cost-vs-node_cost case. The LEFT JOIN pins the
# const lookup SECOND in the join order, so its prefix_cost (174789.72) exceeds
# the full scan's (131968.00) while its own read_cost is 1.00.
_LIVE_CONST_LOOKUP_PLAN = {
    "query_block": {
        "select_id": 1,
        "cost_info": {"query_cost": "174789.72"},
        "nested_loop": [
            {"table": {
                "table_name": "s",
                "access_type": "ALL",
                "rows_examined_per_scan": 1284750,
                "rows_produced_per_join": 428207,
                "filtered": "33.33",
                "cost_info": {"read_cost": "89147.28", "eval_cost": "42820.72",
                              "prefix_cost": "131968.00", "data_read_per_join": "9M"},
                "attached_condition": "(`sampledb`.`s`.`total_price` > 999999.00)",
            }},
            {"table": {
                "table_name": "p",
                "access_type": "const",
                "possible_keys": ["PRIMARY"],
                "key": "PRIMARY",
                "used_key_parts": ["id"],
                "key_length": "4",
                "ref": ["const"],
                "rows_examined_per_scan": 1,
                "rows_produced_per_join": 428207,
                "filtered": "100.00",
                "cost_info": {"read_cost": "1.00", "eval_cost": "42820.72",
                              "prefix_cost": "174789.72", "data_read_per_join": "254M"},
            }},
        ],
    }
}


# SELECT id FROM sales ORDER BY id  -- covering index scan, no filesort.
# Same 1,284,750 rows as the full scan above, but access_type=index: reading a lot
# of rows through an INDEX is not a missing-index problem.
_LIVE_INDEX_SCAN_PLAN = {
    "query_block": {
        "select_id": 1,
        "cost_info": {"query_cost": "131968.00"},
        "ordering_operation": {
            "using_filesort": False,
            "table": {
                "table_name": "sales",
                "access_type": "index",
                "key": "PRIMARY",
                "used_key_parts": ["id"],
                "key_length": "4",
                "rows_examined_per_scan": 1284750,
                "rows_produced_per_join": 1284750,
                "filtered": "100.00",
                "using_index": True,
                "cost_info": {"read_cost": "3493.00", "eval_cost": "128475.00",
                              "prefix_cost": "131968.00", "data_read_per_join": "29M"},
            },
        },
    }
}


def _mysql_cache(plan_doc):
    """A cache whose cluster resolves to aurora-mysql and whose target returns
    `plan_doc`. The plan cell comes back with column name '': measured, the Data
    API reports aliased/expression columns as name '' with the alias in `label`,
    and MySQL's EXPLAIN column is exactly that."""
    cache = MagicMock()
    cache.engine_of.return_value = "aurora-mysql"
    cache.execute_on_target.return_value = QueryResult(
        columns=[""], rows=[{"": json.dumps(plan_doc)}], row_count=1
    )
    return cache


def _pg_cache(plan_doc):
    cache = MagicMock()
    cache.engine_of.return_value = "aurora-postgresql"
    cache.execute_on_target.return_value = QueryResult(
        columns=["QUERY PLAN"], rows=[{"QUERY PLAN": json.dumps([plan_doc])}], row_count=1
    )
    return cache


# --- is_mysql_engine -----------------------------------------------------

def test_is_mysql_engine_matches_both_mysql_families_and_not_sqlserver():
    """Matching standalone `mysql` too is deliberate: both MySQL collectors fill
    table_stats.n_dead_tup from the same DATA_FREE expression. SQL Server is also
    rds_instance and must NOT match."""
    assert is_mysql_engine("aurora-mysql") is True
    assert is_mysql_engine("mysql") is True
    assert is_mysql_engine("aurora-postgresql") is False
    assert is_mysql_engine("sqlserver-ex") is False
    assert is_mysql_engine("docdb") is False
    # Fail-closed toward the historical (PG) path when the engine is unknown.
    assert is_mysql_engine("") is False
    assert is_mysql_engine(None) is False


# --- explain_plan: MySQL ------------------------------------------------

def test_explain_plan_mysql_sends_mysql_statement_not_pg_syntax():
    """The measured bug: the tool unconditionally sent `EXPLAIN (FORMAT JSON,
    VERBOSE)`, which Aurora MySQL rejects with error 1064, so the DBA got a
    generic tool_error. The statement must be MySQL's form."""
    cache = _mysql_cache(_LIVE_JOIN_PLAN)
    result = explain_plan_impl(cache, cluster_id="mysql-1", sql="SELECT id FROM sales")

    stmt = cache.execute_on_target.call_args.args[1]
    assert stmt.startswith("EXPLAIN FORMAT=JSON ")
    assert "(FORMAT JSON" not in stmt  # the PG parenthesized form is what failed
    assert "VERBOSE" not in stmt.upper()
    assert "ANALYZE" not in stmt.upper()
    assert result["status"] == "ok"
    assert result["engine"] == "mysql"


def test_explain_plan_mysql_finds_full_scan_filesort_and_cost_on_live_plan():
    cache = _mysql_cache(_LIVE_JOIN_PLAN)
    result = explain_plan_impl(cache, cluster_id="mysql-1", sql="SELECT 1 FROM sales")

    issues = {f["issue"]: f for f in result["findings"]}
    # access_type=ALL over 1,284,750 rows on the driving table.
    assert issues["Full table scan on large table"]["severity"] == "high"
    assert issues["Full table scan on large table"]["relation"] == "s"
    # filtered=33.33% -> reads 1.28M rows to keep an estimated 428,207.
    assert issues["Low filter selectivity"]["relation"] == "s"
    assert "428207" in issues["Low filter selectivity"]["detail"]
    # ordering_operation.using_filesort lives on the CONTAINER, not the table.
    assert issues["Sort not served by an index (filesort)"]["severity"] == "medium"
    # query_cost 602,995.88 >= 100,000.
    assert issues["High total plan cost"]["severity"] == "info"
    # Both access nodes were walked out of the nested_loop list.
    assert result["summary"]["node_count"] == 2
    assert result["summary"]["total_cost"] == 602995.88
    assert result["summary"]["estimated_rows"] == 428207.0
    # Ranked by each node's OWN cost (read_cost + eval_cost), which for this plan
    # is p 428207.17+42820.72 = 471027.89 against s 89147.28+42820.72 = 131968.00.
    # The eq_ref on p is genuinely the expensive access here: it is re-probed once
    # per row s produces, which is what its read_cost already prices in.
    assert [n["relation"] for n in result["expensive_nodes"]] == ["p", "s"]
    assert result["expensive_nodes"][0]["key"] == "PRIMARY"
    assert result["expensive_nodes"][0]["node_cost"] == 471027.89
    assert result["expensive_nodes"][1]["node_cost"] == 131968.0
    # The two components stay visible, and the cumulative figure is gone.
    assert result["expensive_nodes"][0]["read_cost"] == 428207.17
    assert result["expensive_nodes"][0]["eval_cost"] == 42820.72
    assert "total_cost" not in result["expensive_nodes"][0]
    assert "prefix_cost" not in result["expensive_nodes"][0]


def test_explain_plan_mysql_finds_temporary_table_nested_two_levels_deep():
    """using_temporary_table sits under ordering_operation -> grouping_operation.
    A walker that only knew the wrapper keys it had seen would drop it."""
    cache = _mysql_cache(_LIVE_GROUP_PLAN)
    result = explain_plan_impl(cache, cluster_id="mysql-1", sql="SELECT 1 FROM sales")

    issues = {f["issue"] for f in result["findings"]}
    assert "Internal temporary table" in issues
    assert "Sort not served by an index (filesort)" in issues
    # filtered=100% on this plan: the selectivity finding must NOT fire.
    assert "Low filter selectivity" not in issues
    assert result["summary"]["node_count"] == 1


def test_explain_plan_mysql_does_not_call_an_index_scan_a_full_table_scan():
    """Live plan: access_type=index over the SAME 1,284,750 rows as the full scan.
    Reading many rows THROUGH an index is not a missing-index finding, and calling
    it one would send a DBA to add an index that already exists."""
    cache = _mysql_cache(_LIVE_INDEX_SCAN_PLAN)
    result = explain_plan_impl(cache, cluster_id="mysql-1", sql="SELECT id FROM sales")

    issues = {f["issue"] for f in result["findings"]}
    assert "Full table scan on large table" not in issues
    assert "Sort not served by an index (filesort)" not in issues  # using_filesort=false
    assert "Low filter selectivity" not in issues                  # filtered=100%
    # Only the cost note remains, and the node is still reported.
    assert issues == {"High total plan cost"}
    assert result["expensive_nodes"][0]["relation"] == "sales"
    assert result["expensive_nodes"][0]["node_type"] == "index"


def test_explain_plan_mysql_ranks_a_full_scan_above_a_one_row_const_lookup():
    """The prefix_cost defect, on a live plan captured for exactly this shape.

    prefix_cost is the cumulative cost of the join prefix THROUGH this table, so
    it is non-decreasing along the join order and ranking by it just returns the
    reversed join order. Here that puts p, a single-row const PRIMARY lookup with
    read_cost 1.00, above s, a 1,284,750-row full table scan: the DBA is pointed
    at the cheapest access in the plan. Ranking by the node's own read + eval
    cost puts the full scan first, where it belongs."""
    cache = _mysql_cache(_LIVE_CONST_LOOKUP_PLAN)
    result = explain_plan_impl(cache, cluster_id="mysql-1", sql="SELECT 1 FROM sales")

    nodes = result["expensive_nodes"]
    assert [n["relation"] for n in nodes] == ["s", "p"]
    # s: 89147.28 + 42820.72. p: 1.00 + 42820.72.
    assert nodes[0]["node_cost"] == 131968.0
    assert nodes[1]["node_cost"] == 42821.72
    # Reversed join order is what prefix_cost would have produced (131968.00 then
    # 174789.72), so this ordering is the whole point of the change.
    assert nodes[0]["node_type"] == "ALL" and nodes[1]["node_type"] == "const"
    # Both row figures, under MySQL's own names: 1 row per scan of p, yet 428,207
    # rows evaluated through it, which is why its eval_cost is 42820.72 and its
    # read_cost is 1.00. A single "plan_rows" next to a cost cannot say that.
    assert nodes[1]["rows_examined_per_scan"] == 1.0
    assert nodes[1]["rows_produced_per_join"] == 428207.0
    assert nodes[1]["read_cost"] == 1.0 and nodes[1]["eval_cost"] == 42820.72


def test_explain_plan_mysql_states_what_it_cannot_analyze():
    """MySQL's plan-only EXPLAIN carries no planning time and no actual row
    counts. Those must be named as unavailable, not defaulted to a number that
    reads like a measurement."""
    cache = _mysql_cache(_LIVE_JOIN_PLAN)
    result = explain_plan_impl(cache, cluster_id="mysql-1", sql="SELECT 1 FROM sales")

    assert result["summary"]["planning_time_ms"] is None
    unavailable = result["unavailable_analysis"]
    assert set(unavailable) == {"planning_time_ms", "row_estimate_miss", "disk_spill"}
    # The PG-only heuristics must not appear as MySQL findings.
    issues = {f["issue"] for f in result["findings"]}
    assert not any("estimate off by" in i for i in issues)
    assert "Operation spilled to disk" not in issues


def test_explain_plan_mysql_refuses_analyze_without_touching_the_target():
    """analyze=true EXECUTES the statement, and MySQL's EXPLAIN ANALYZE returns
    no JSON to parse, so there is nothing to gain by running it. Refuse BEFORE
    the target call."""
    cache = _mysql_cache(_LIVE_JOIN_PLAN)
    result = explain_plan_impl(
        cache, cluster_id="mysql-1", sql="SELECT id FROM sales", analyze=True
    )
    assert result["status"] == "rejected"
    assert result["engine"] == "mysql"
    assert "EXPLAIN ANALYZE" in result["reason"]
    cache.execute_on_target.assert_not_called()


def test_explain_plan_mysql_records_plan_history_under_mysql_structure():
    """Plan-flip detection must work on MySQL too: the signature is (access_type,
    table, key) and a changed access path must read as a flip."""
    cache = _mysql_cache(_LIVE_JOIN_PLAN)
    cache.execute.side_effect = [
        QueryResult(["plan_hash", "captured_at"],
                    [{"plan_hash": "OLD", "captured_at": "2026-07-01T00:00:00Z"}], 1),
        {},  # INSERT
    ]
    result = explain_plan_impl(cache, cluster_id="mysql-1", sql="SELECT 1 FROM sales")
    assert result["plan_change"]["changed"] is True
    assert "flip" in result["plan_change"]["note"].lower()


# --- explain_plan: PG regression ---------------------------------------

def test_explain_plan_pg_path_is_unchanged():
    """Pin the PG path: same statement, same findings, same summary keys."""
    plan_doc = {
        "Plan": {"Node Type": "Seq Scan", "Relation Name": "orders",
                 "Plan Rows": 50000, "Total Cost": 1234.5},
        "Planning Time": 0.42,
    }
    cache = _pg_cache(plan_doc)
    result = explain_plan_impl(cache, cluster_id="pg-1", sql="SELECT * FROM orders")

    stmt = cache.execute_on_target.call_args.args[1]
    assert stmt == "EXPLAIN (FORMAT JSON, VERBOSE) SELECT * FROM orders"
    assert result["engine"] == "postgresql"
    assert result["summary"]["planning_time_ms"] == 0.42
    assert [f["issue"] for f in result["findings"]] == ["Sequential scan on large table"]
    assert "unavailable_analysis" not in result


def test_explain_plan_unresolvable_engine_keeps_the_pg_path():
    """engine_of() returns '' when cluster_meta has no row or the lookup fails.
    That must degrade to the behaviour this tool always had, not to MySQL syntax
    (the handler gate has already refused genuinely unresolvable clusters)."""
    plan_doc = {"Plan": {"Node Type": "Result", "Plan Rows": 1, "Total Cost": 0.01}}
    cache = _pg_cache(plan_doc)
    cache.engine_of.return_value = ""
    result = explain_plan_impl(cache, cluster_id="unknown-1", sql="SELECT 1")
    assert result["engine"] == "postgresql"
    assert "(FORMAT JSON" in cache.execute_on_target.call_args.args[1]


def test_explain_plan_pg_still_refuses_non_select_on_both_engines():
    for engine in ("aurora-postgresql", "aurora-mysql"):
        cache = MagicMock()
        cache.engine_of.return_value = engine
        result = explain_plan_impl(cache, cluster_id="c1", sql="DELETE FROM orders")
        assert result["status"] == "rejected", engine
        cache.execute_on_target.assert_not_called()


# --- get_vacuum_stats ---------------------------------------------------

def _table_stats_result():
    """The two live Aurora MySQL rows, as the cache SQL returns them.
    Measured 2026-07-28: products 963,662 live / 108,473 free-rows (11.26%),
    sales 1,284,750 / 119,156 (9.27%). The MySQL collectors write NULL for
    last_vacuum/last_analyze."""
    return QueryResult(
        columns=["schemaname", "table_name", "dead_tuples", "live_tuples", "bloat_pct",
                 "last_vacuum", "last_analyze"],
        rows=[
            {"schemaname": "sampledb", "table_name": "products", "dead_tuples": 108473,
             "live_tuples": 963662, "bloat_pct": 11.26, "last_vacuum": None,
             "last_analyze": None},
            {"schemaname": "sampledb", "table_name": "sales", "dead_tuples": 119156,
             "live_tuples": 1284750, "bloat_pct": 9.27, "last_vacuum": None,
             "last_analyze": None},
        ],
        row_count=2,
    )


def test_vacuum_stats_mysql_relabels_innodb_numbers_and_drops_vacuum_fields():
    """The measured lie: this tool reported products as 108,473 "dead tuples" with
    11.26% "bloat" and last_vacuum NULL. InnoDB has none of those three things."""
    cache = MagicMock()
    cache.engine_of.return_value = "aurora-mysql"
    cache.execute.return_value = _table_stats_result()

    result = get_vacuum_stats_impl(cache, cluster_id="mysql-1")

    assert result["engine"] == "mysql"
    row = result["tables"][0]
    assert row["table_name"] == "products"
    assert row["free_rows_est"] == 108473
    assert row["fragmentation_pct"] == 11.26
    # The PG names must be gone, not merely supplemented.
    for gone in ("dead_tuples", "bloat_pct", "last_vacuum", "last_analyze"):
        assert gone not in row, gone
    # And the response must say what the number actually is.
    assert "DATA_FREE" in result["source"]
    assert "OPTIMIZE TABLE" in result["source"]


def test_vacuum_stats_mysql_is_silent_at_normal_innodb_free_space():
    """11.26% / 9.27% reclaimable space is ordinary free-list churn. OPTIMIZE
    TABLE rebuilds the whole table, so warning here would be a false positive."""
    cache = MagicMock()
    cache.engine_of.return_value = "aurora-mysql"
    cache.execute.return_value = _table_stats_result()
    result = get_vacuum_stats_impl(cache, cluster_id="mysql-1")
    assert result["warnings"] == []
    assert result["threshold_pct"] == 25.0


def test_vacuum_stats_mysql_warns_above_threshold_with_optimize_table():
    cache = MagicMock()
    cache.engine_of.return_value = "aurora-mysql"
    cache.execute.return_value = QueryResult(
        columns=["schemaname", "table_name", "dead_tuples", "live_tuples", "bloat_pct",
                 "last_vacuum", "last_analyze"],
        rows=[{"schemaname": "app", "table_name": "purge_log", "dead_tuples": 900000,
               "live_tuples": 1000000, "bloat_pct": 90.0, "last_vacuum": None,
               "last_analyze": None}],
        row_count=1,
    )
    result = get_vacuum_stats_impl(cache, cluster_id="mysql-1")
    assert len(result["warnings"]) == 1
    assert "purge_log" in result["warnings"][0]
    assert "OPTIMIZE TABLE" in result["warnings"][0]
    assert "VACUUM" not in result["warnings"][0]
    assert "dead tuple" not in result["warnings"][0].lower()


def test_vacuum_stats_pg_keeps_pg_names_and_bloat_warning():
    cache = MagicMock()
    cache.engine_of.return_value = "aurora-postgresql"
    cache.execute.return_value = QueryResult(
        columns=["table_name", "dead_tuples", "live_tuples", "bloat_pct"],
        rows=[
            {"table_name": "orders", "dead_tuples": 50000, "live_tuples": 100000,
             "bloat_pct": 50.0},
            {"table_name": "users", "dead_tuples": 100, "live_tuples": 10000,
             "bloat_pct": 1.0},
        ],
        row_count=2,
    )
    result = get_vacuum_stats_impl(cache, cluster_id="pg-1")
    assert result["engine"] == "postgresql"
    assert len(result["warnings"]) == 1
    assert "dead tuples" in result["warnings"][0]
    assert result["tables"][0]["bloat_pct"] == 50.0
    assert "source" not in result


def test_vacuum_stats_reads_the_same_cluster_scoped_cache_for_both_engines():
    for engine in ("aurora-postgresql", "aurora-mysql"):
        cache = MagicMock()
        cache.engine_of.return_value = engine
        cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
        get_vacuum_stats_impl(cache, cluster_id="c1")
        sql, params = cache.execute.call_args.args
        assert "table_stats" in sql, engine
        assert "cluster_id = :cluster_id" in sql, engine
        assert params["cluster_id"] == "c1", engine


@pytest.mark.parametrize("engine", [
    "sqlserver-ex",     # rds_instance, no table_stats producer (MySQL-only there)
    "docdb",            # documentdb
    "dynamodb",
    "redis",            # elasticache
    "",                 # engine could not be resolved at all
])
def test_vacuum_stats_refuses_every_engine_that_has_no_table_stats(engine):
    """Measured against the live registry: dbops-demo-mssql, dbops-docdb-test,
    ddb-0d089ec02d21 and dbops-test-valkey were each told engine='postgresql'
    with tables=[] and warnings=[]. A false all-clear under a false engine label,
    which is the exact defect class this tier set out to remove. get_vacuum_stats
    is not in the handler's _ENGINE_GATED_TOOLS, so the refusal lives in the tool."""
    cache = MagicMock()
    cache.engine_of.return_value = engine

    result = get_vacuum_stats_impl(cache, cluster_id="c1")

    assert result["status"] == "unsupported_engine"
    # No engine label may be asserted, least of all the wrong one.
    assert result.get("engine") != "postgresql"
    assert "tables" not in result and "warnings" not in result
    # An empty result must not read as "nothing to clean up".
    assert "정리할 것이 없다" in result["reason"]
    # And it must not have queried the cache at all.
    cache.execute.assert_not_called()


@pytest.mark.parametrize("engine", ["aurora-postgresql", "postgres", "aurora-mysql", "mysql"])
def test_vacuum_stats_still_answers_for_both_supported_engines(engine):
    cache = MagicMock()
    cache.engine_of.return_value = engine
    cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    result = get_vacuum_stats_impl(cache, cluster_id="c1")
    assert result.get("status") != "unsupported_engine", engine
    assert result["engine"] == ("mysql" if "mysql" in engine else "postgresql")


# --- recommend_index ----------------------------------------------------

def test_recommend_index_mysql_refuses_instead_of_reporting_zero():
    """Measured: MySQL query_stats rows have shared_blks_read / shared_blks_hit
    100% NULL (2,889 rows over 24h), so the candidate filter excluded every row
    and the tool returned count: 0, a false all-clear. It must refuse, and it
    must not carry a zero count anyone can misread as a result."""
    cache = MagicMock()
    cache.engine_of.return_value = "aurora-mysql"

    result = recommend_index_impl(cache, cluster_id="mysql-1")

    assert result["status"] == "unsupported_engine"
    assert result["engine"] == "mysql"
    assert "recommendations" not in result
    assert "count" not in result
    # The refusal must not read as "no index needed".
    assert "explain_plan" in result["reason"]
    # And it must not have run the PG query against the cache.
    cache.execute.assert_not_called()


def test_recommend_index_pg_path_still_runs_its_query():
    cache = MagicMock()
    cache.engine_of.return_value = "aurora-postgresql"
    cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    result = recommend_index_impl(cache, cluster_id="pg-1")
    assert result["count"] == 0
    assert result["recommendations"] == []
    sql = cache.execute.call_args_list[0].args[0]
    assert "query_stats" in sql
    assert "shared_blks_read" in sql
