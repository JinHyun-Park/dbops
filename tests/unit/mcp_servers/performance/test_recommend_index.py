from unittest.mock import MagicMock

from mcp_servers.performance.tools.recommend_index import recommend_index_impl
from mcp_servers.shared.models import QueryResult

# The impl issues TWO cache.execute calls in order:
#   1) query_stats  (the heavy-query workload)
#   2) table_stats  (optional corroboration)
# Tests drive both via side_effect; when table_stats corroboration is irrelevant
# we return an empty QueryResult for the second call.
_EMPTY = QueryResult(columns=[], rows=[], row_count=0)


def _qstats(rows):
    return QueryResult(
        columns=["query_hash", "query_text", "total_time_ms", "calls", "blocks_read", "blocks_hit"],
        rows=rows,
        row_count=len(rows),
    )


def test_where_equality_emits_create_index_ddl():
    """A WHERE equality + range query should yield a composite index leading with
    the equality column, then the range column, as actual CONCURRENTLY DDL."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        _qstats([{
            "query_hash": "h1",
            "query_text": "SELECT * FROM orders WHERE status = 'pending' AND created_at > now()",
            "total_time_ms": 5000.0,
            "calls": 100,
            "blocks_read": 5000,
            "blocks_hit": 10,
        }]),
        _EMPTY,
    ]
    result = recommend_index_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["count"] == 1
    rec = result["recommendations"][0]
    assert rec["table"] == "orders"
    assert rec["columns"] == ["status", "created_at"]
    assert "CREATE INDEX" in rec["ddl"]
    assert "CONCURRENTLY" in rec["ddl"]
    assert "ON orders (status, created_at)" in rec["ddl"]


def test_join_key_column_captured():
    """A JOIN query should capture the driving-table side of the equi-join key."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        _qstats([{
            "query_hash": "h2",
            "query_text": (
                "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id"
            ),
            "total_time_ms": 3000.0,
            "calls": 50,
            "blocks_read": 4000,
            "blocks_hit": 10,
        }]),
        _EMPTY,
    ]
    result = recommend_index_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["count"] == 1
    rec = result["recommendations"][0]
    assert rec["table"] == "orders"
    assert "customer_id" in rec["columns"]
    assert "ON orders (customer_id)" in rec["ddl"]


def test_unparseable_query_is_skipped():
    """A query with no FROM/WHERE (nothing to index) must NOT emit garbage DDL."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        _qstats([{
            "query_hash": "h3",
            "query_text": "SELECT now()",
            "total_time_ms": 1000.0,
            "calls": 10,
            "blocks_read": 2000,
            "blocks_hit": 1,
        }]),
        _EMPTY,
    ]
    result = recommend_index_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["count"] == 0
    assert result["recommendations"] == []


def test_empty_query_stats_returns_no_recommendations():
    """No heavy queries → empty recommendations, with the note still present."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [_EMPTY, _EMPTY]
    result = recommend_index_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["recommendations"] == []
    assert result["count"] == 0
    assert "heuristic" in result["note"].lower()


def test_duplicate_snapshots_merge_into_one_recommendation():
    """Two snapshots of the same query → ONE merged recommendation with calls and
    total_time_ms summed."""
    mock_cache = MagicMock()
    same_query = "SELECT * FROM orders WHERE status = 'pending'"
    mock_cache.execute.side_effect = [
        _qstats([
            {
                "query_hash": "h4",
                "query_text": same_query,
                "total_time_ms": 2000.0,
                "calls": 40,
                "blocks_read": 3000,
                "blocks_hit": 10,
            },
            {
                "query_hash": "h4",
                "query_text": same_query,
                "total_time_ms": 1000.0,
                "calls": 60,
                "blocks_read": 1500,
                "blocks_hit": 10,
            },
        ]),
        _EMPTY,
    ]
    result = recommend_index_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["count"] == 1
    rec = result["recommendations"][0]
    assert rec["total_time_ms"] == 3000.0
    assert rec["calls"] == 100


def test_table_stats_annotates_and_confirms_seq_scan():
    """When table_stats shows seq_scan dominating idx_scan, the recommendation is
    annotated and flagged confirmed."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        _qstats([{
            "query_hash": "h5",
            "query_text": "SELECT * FROM orders WHERE status = 'pending'",
            "total_time_ms": 5000.0,
            "calls": 100,
            "blocks_read": 5000,
            "blocks_hit": 10,
        }]),
        QueryResult(
            columns=["schema_name", "table_name", "seq_scan", "idx_scan", "n_live_tup"],
            rows=[{
                "schema_name": "public",
                "table_name": "orders",
                "seq_scan": 9000,
                "idx_scan": 100,
                "n_live_tup": 1_000_000,
            }],
            row_count=1,
        ),
    ]
    result = recommend_index_impl(mock_cache, cluster_id="prod-pg-1")
    rec = result["recommendations"][0]
    assert rec["seq_scan"] == 9000
    assert rec["idx_scan"] == 100
    assert rec["seq_scan_confirmed"] is True


# --- Issue #1: CTE / subquery / derived-table shapes must be skipped entirely ---


def test_cte_query_yields_no_recommendation():
    """A WITH/CTE query is skipped — we can't attribute inner columns to a concrete
    base table, and indexing the CTE name would be invalid."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        _qstats([{
            "query_hash": "c1",
            "query_text": (
                "WITH recent AS (SELECT * FROM orders WHERE status = 'pending') "
                "SELECT * FROM recent WHERE total > 100"
            ),
            "total_time_ms": 5000.0,
            "calls": 100,
            "blocks_read": 5000,
            "blocks_hit": 10,
        }]),
        _EMPTY,
    ]
    result = recommend_index_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["count"] == 0
    assert result["recommendations"] == []


def test_subquery_in_where_not_attributed_to_driving_table():
    """A WHERE-clause subquery (`id IN (SELECT ...)`) must NOT leak the inner
    column into the driving table's index. We skip the whole query."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        _qstats([{
            "query_hash": "s1",
            "query_text": (
                "SELECT * FROM orders WHERE customer_id IN "
                "(SELECT id FROM customers WHERE region = 'EU')"
            ),
            "total_time_ms": 4000.0,
            "calls": 80,
            "blocks_read": 4000,
            "blocks_hit": 10,
        }]),
        _EMPTY,
    ]
    result = recommend_index_impl(mock_cache, cluster_id="prod-pg-1")
    # Must not emit any recommendation that references the inner subquery columns.
    for rec in result["recommendations"]:
        assert "region" not in rec["columns"]
        assert "id" not in rec["columns"]
    # Conservative behavior: any subquery → skip the whole query.
    assert result["count"] == 0


# --- Issue #2: ORDER BY positional / expression tokens must not become columns ---


def test_order_by_positional_not_turned_into_column():
    """`ORDER BY 1` is positional — never emit a column named '1'. Here the WHERE
    equality still produces a valid recommendation, but ORDER BY adds nothing."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        _qstats([{
            "query_hash": "o1",
            "query_text": "SELECT status, count(*) FROM orders WHERE status = 'x' ORDER BY 1",
            "total_time_ms": 3000.0,
            "calls": 50,
            "blocks_read": 3000,
            "blocks_hit": 10,
        }]),
        _EMPTY,
    ]
    result = recommend_index_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["count"] == 1
    rec = result["recommendations"][0]
    assert rec["columns"] == ["status"]
    assert "1" not in rec["columns"]


def test_order_by_expression_not_turned_into_column():
    """`ORDER BY lower(email)` is an expression — never emit an expression index.
    The WHERE equality column stands; the ORDER BY expression is dropped."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        _qstats([{
            "query_hash": "o2",
            "query_text": "SELECT * FROM users WHERE active = true ORDER BY lower(email)",
            "total_time_ms": 3000.0,
            "calls": 50,
            "blocks_read": 3000,
            "blocks_hit": 10,
        }]),
        _EMPTY,
    ]
    result = recommend_index_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["count"] == 1
    rec = result["recommendations"][0]
    assert rec["columns"] == ["active"]
    assert "lower" not in rec["columns"]
    assert "email" not in rec["columns"]
    assert rec["ddl"] == "CREATE INDEX CONCURRENTLY idx_users_active ON users (active);"


# --- Issue #3: quoted / reserved / non-simple identifiers must be skipped ---


def test_quoted_reserved_identifiers_are_skipped():
    """`SELECT * FROM "User" WHERE "order" = 1` — quoted/reserved identifiers would
    case-fold or be invalid if we stripped the quotes, so the query is skipped and
    NO DDL is emitted."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        _qstats([{
            "query_hash": "q1",
            "query_text": 'SELECT * FROM "User" WHERE "order" = 1',
            "total_time_ms": 5000.0,
            "calls": 100,
            "blocks_read": 5000,
            "blocks_hit": 10,
        }]),
        _EMPTY,
    ]
    result = recommend_index_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["count"] == 0
    assert result["recommendations"] == []


def test_reserved_word_column_dropped_but_simple_columns_kept():
    """If only SOME columns are non-simple, drop those and keep the rest. Here a
    bare reserved word in a predicate must not become a column, but the simple
    column does."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        _qstats([{
            "query_hash": "q2",
            "query_text": 'SELECT * FROM orders WHERE status = \'x\' AND "user" = 5',
            "total_time_ms": 5000.0,
            "calls": 100,
            "blocks_read": 5000,
            "blocks_hit": 10,
        }]),
        _EMPTY,
    ]
    result = recommend_index_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["count"] == 1
    rec = result["recommendations"][0]
    assert rec["columns"] == ["status"]
    assert "user" not in rec["columns"]


def test_order_by_select_list_alias_is_not_indexed():
    """A bare ORDER BY token that is actually a SELECT-list alias (not a base
    column) must NOT become an index column — `ORDER BY` only trusts columns
    qualified by the driving alias. WHERE columns still come through."""
    from mcp_servers.performance.tools.recommend_index import _parse_query

    parsed = _parse_query(
        "SELECT lower(email) AS email_key FROM users WHERE status = 1 ORDER BY email_key"
    )
    assert parsed is not None
    assert parsed["columns"] == ["status"]
    assert "email_key" not in parsed["columns"]


def test_recommendation_carries_unverified_verification_sql():
    """Each rec is flagged unverified and carries runnable verification SQL
    (existing-index check) so the DBA can rule out a duplicate before creating."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        _qstats([{
            "query_hash": "v1",
            "query_text": "SELECT * FROM orders WHERE status = 'pending'",
            "total_time_ms": 5000.0, "calls": 100, "blocks_read": 5000, "blocks_hit": 10,
        }]),
        _EMPTY,
    ]
    rec = recommend_index_impl(mock_cache, cluster_id="prod-pg-1")["recommendations"][0]
    assert rec["validated"] is False
    assert "pg_indexes" in rec["verification"]["check_existing_indexes"]
    assert "tablename = 'orders'" in rec["verification"]["check_existing_indexes"]


def test_prefix_index_is_deduped_into_composite():
    """A rec on (status) and a rec on (status, created_at) for the same table:
    the shorter is subsumed by the composite (prefix), so only ONE survives and
    the dropped one's workload is folded in + recorded in `covers`."""
    mock_cache = MagicMock()
    mock_cache.execute.side_effect = [
        _qstats([
            {  # -> (status, created_at)
                "query_hash": "p1",
                "query_text": "SELECT * FROM orders WHERE status = 'x' AND created_at > now()",
                "total_time_ms": 3000.0, "calls": 30, "blocks_read": 5000, "blocks_hit": 10,
            },
            {  # -> (status) — a prefix of the composite above
                "query_hash": "p2",
                "query_text": "SELECT * FROM orders WHERE status = 'y'",
                "total_time_ms": 1000.0, "calls": 70, "blocks_read": 5000, "blocks_hit": 10,
            },
        ]),
        _EMPTY,
    ]
    result = recommend_index_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["count"] == 1
    rec = result["recommendations"][0]
    assert rec["columns"] == ["status", "created_at"]
    assert rec["calls"] == 100  # 30 + 70 folded in
    assert rec["total_time_ms"] == 4000.0
    assert [["status"]] == rec["covers"]


def test_order_by_qualified_column_is_indexed():
    """A driving-alias-qualified ORDER BY column IS a proven base column."""
    from mcp_servers.performance.tools.recommend_index import _parse_query

    parsed = _parse_query(
        "SELECT * FROM orders o WHERE o.status = 1 ORDER BY o.created_at"
    )
    assert parsed["columns"] == ["status", "created_at"]


def test_order_by_expression_is_not_indexed():
    """An ORDER BY expression that merely STARTS with a column (e.g.
    `o.created_at + interval '1 day'`) must not be reduced to a column index —
    the whole ORDER BY item must be a plain column (+ optional ASC/DESC/NULLS)."""
    from mcp_servers.performance.tools.recommend_index import _parse_query

    parsed = _parse_query(
        "SELECT * FROM orders o WHERE o.status = 1 ORDER BY o.created_at + interval '1 day'"
    )
    assert parsed["columns"] == ["status"]
    # plain qualified column with direction modifiers IS still accepted
    parsed2 = _parse_query(
        "SELECT * FROM orders o WHERE o.status = 1 ORDER BY o.created_at DESC NULLS LAST"
    )
    assert parsed2["columns"] == ["status", "created_at"]
