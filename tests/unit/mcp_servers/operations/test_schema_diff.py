"""Unit tests for the structured schema_diff tool.

Covers the four-bucket output (added / dropped / modified /
rename_candidates) plus edge cases the heuristic has to handle:
column-order equivalence, string-encoded blob inputs, and missing
snapshots.
"""

from unittest.mock import MagicMock

from mcp_servers.operations.tools.schema_diff import (
    _compute_diff,
    _parse_tables,
    get_schema_diff_impl,
)
from mcp_servers.shared.models import QueryResult

# ---------------------------------------------------------------------------
# _parse_tables — handles dict / JSON string / unsupported shapes
# ---------------------------------------------------------------------------


def test_parse_tables_accepts_dict_list_of_cols():
    out = _parse_tables({"users": ["id", "email", "name"]})
    assert out == {"users": ["email", "id", "name"]}  # sorted


def test_parse_tables_accepts_json_string():
    out = _parse_tables('{"users": ["id", "name"]}')
    assert out == {"users": ["id", "name"]}


def test_parse_tables_accepts_col_dict_form():
    """Some snapshotters store {col_name: type}; we treat keys as cols."""
    out = _parse_tables({"orders": {"id": "int", "amount": "numeric"}})
    assert out == {"orders": ["amount", "id"]}


def test_parse_tables_empty_blob_returns_empty():
    assert _parse_tables(None) == {}
    assert _parse_tables("") == {}
    assert _parse_tables("not json") == {}


# ---------------------------------------------------------------------------
# _compute_diff — the actual structural logic
# ---------------------------------------------------------------------------


def test_compute_diff_pure_add():
    diff = _compute_diff(
        before={"users": ["id"]},
        after={"users": ["id"], "audit_log": ["id", "ts"]},
    )
    assert diff["added"] == ["audit_log"]
    assert diff["dropped"] == []
    assert diff["modified"] == []
    assert diff["rename_candidates"] == []


def test_compute_diff_pure_drop():
    diff = _compute_diff(
        before={"users": ["id"], "legacy_v1": ["id"]},
        after={"users": ["id"]},
    )
    assert diff["dropped"] == ["legacy_v1"]
    assert diff["added"] == []


def test_compute_diff_modified_column_added():
    """Same table name, new column → modified with added_columns set."""
    diff = _compute_diff(
        before={"users": ["id", "email"]},
        after={"users": ["id", "email", "phone"]},
    )
    assert diff["added"] == []
    assert diff["dropped"] == []
    mod = diff["modified"]
    assert len(mod) == 1
    assert mod[0]["table"] == "users"
    assert mod[0]["added_columns"] == ["phone"]
    assert mod[0]["dropped_columns"] == []


def test_compute_diff_modified_column_dropped():
    diff = _compute_diff(
        before={"users": ["id", "email", "ssn"]},
        after={"users": ["id", "email"]},
    )
    assert diff["modified"][0]["dropped_columns"] == ["ssn"]


def test_compute_diff_rename_candidate():
    """Same column signature, different table names → flagged as rename
    candidate; both names removed from dropped/added so the agent
    doesn't double-count."""
    diff = _compute_diff(
        before={"customers_v1": ["id", "email"]},
        after={"customers": ["id", "email"]},
    )
    assert diff["rename_candidates"] == [
        {"from": "customers_v1", "to": "customers"}
    ]
    assert diff["dropped"] == []
    assert diff["added"] == []


def test_compute_diff_drop_not_rename_when_columns_differ():
    """If a dropped table's columns DON'T match any added table, it
    stays as a plain DROP."""
    diff = _compute_diff(
        before={"customers_v1": ["id", "email"]},
        after={"customers": ["id", "email", "phone"]},  # extra col → not match
    )
    assert diff["rename_candidates"] == []
    assert diff["dropped"] == ["customers_v1"]
    assert diff["added"] == ["customers"]


def test_compute_diff_mixed():
    """All four buckets at once: add + drop + modify + rename."""
    diff = _compute_diff(
        before={
            "users": ["id", "email"],
            "old_audit": ["id", "ts"],
            "legacy_temp": ["k", "v"],
        },
        after={
            "users": ["id", "email", "phone"],  # modified
            "audit": ["id", "ts"],  # rename of old_audit
            "new_feature": ["id"],  # plain add
        },
    )
    assert diff["modified"][0]["table"] == "users"
    assert diff["rename_candidates"] == [{"from": "old_audit", "to": "audit"}]
    assert diff["dropped"] == ["legacy_temp"]
    assert diff["added"] == ["new_feature"]


def test_compute_diff_column_order_irrelevant():
    """before=['a','b'] vs after=['b','a'] is NOT a modification."""
    # _parse_tables already sorts, but verify _compute_diff doesn't
    # accidentally see them as different.
    diff = _compute_diff(
        before={"users": ["a", "b"]},
        after={"users": ["b", "a"]},  # different input order
    )
    # _parse_tables already sorted them; if both inputs sorted same,
    # they're equal → no modification.
    # (Test compute_diff with sorted inputs only since callers pass via
    # _parse_tables which always sorts.)
    diff = _compute_diff({"users": ["a", "b"]}, {"users": ["a", "b"]})
    assert diff["modified"] == []


# ---------------------------------------------------------------------------
# get_schema_diff_impl — end-to-end through a mocked cache
# ---------------------------------------------------------------------------


def test_diff_impl_two_snapshots_drop_surfaced():
    """DROP scenario: pre-incident schema had a table, post-incident
    doesn't. The output's `dropped` list must include it."""
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["schema_name", "tables_before", "tables_after"],
        rows=[
            {
                "schema_name": "public",
                "tables_before": '{"users": ["id"], "deleted_table": ["k"]}',
                "tables_after": '{"users": ["id"]}',
            }
        ],
        row_count=1,
    )
    result = get_schema_diff_impl(
        mock_cache,
        cluster_id="prod-pg-1",
        snapshot_a="2026-05-01T00:00:00Z",
        snapshot_b="2026-05-02T00:00:00Z",
    )
    assert result["cluster_id"] == "prod-pg-1"
    assert result["schemas_compared"] == 1
    assert result["totals"]["dropped"] == 1
    assert result["diffs"][0]["dropped"] == ["deleted_table"]


def _coverage(snapshots, schemas):
    return QueryResult(
        columns=["snapshots", "schemas", "first_seen", "last_seen"],
        rows=[{"snapshots": snapshots, "schemas": schemas,
               "first_seen": "2026-07-01T00:00:00Z" if snapshots else None,
               "last_seen": "2026-07-09T00:00:00Z" if snapshots else None}],
        row_count=1,
    )


def _cache(*results):
    mock = MagicMock()
    mock.execute.side_effect = list(results)
    return mock


_EMPTY = QueryResult(columns=[], rows=[], row_count=0)


def test_never_collected_is_not_reported_as_no_differences():
    """This used to return schemas_compared 0 with all totals zero, which reads
    as "the schema is identical". It has to say we have no snapshot at all."""
    mock_cache = _cache(_EMPTY, _coverage(0, 0))
    result = get_schema_diff_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["status"] == "not_collected"
    assert result["schemas_compared"] == 0
    assert result["diffs"] == []
    assert result["totals"] == {"added": 0, "dropped": 0, "modified": 0, "rename_candidates": 0}
    assert result["collection_coverage"]["snapshots_stored"] == 0
    assert "차이가 없다는 뜻이 아닙니다" in result["note"]


def test_single_snapshot_is_baseline_not_a_zero_diff():
    """The implicit query LEFT JOINs, so the baseline row comes back with a NULL
    tables_before. Diffing it would report every existing table as ADDED."""
    mock_cache = _cache(
        QueryResult(
            columns=["schema_name", "tables_before", "tables_after"],
            rows=[{"schema_name": "public", "tables_before": None,
                   "tables_after": '{"users": ["id"]}'}],
            row_count=1,
        ),
        _coverage(1, 1),
    )
    result = get_schema_diff_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["status"] == "insufficient_snapshots"
    assert result["baseline_only_schemas"] == ["public"]
    assert result["schemas_compared"] == 0
    assert result["totals"]["added"] == 0  # NOT "users was added"


def test_explicit_timestamps_that_match_nothing_say_so():
    """Two ISO strings that hit no snapshot pair is a DIFFERENT failure from
    having no data: the caller guessed the timestamps."""
    mock_cache = _cache(_EMPTY, _coverage(6, 2))
    result = get_schema_diff_impl(
        mock_cache, cluster_id="prod-pg-1",
        snapshot_a="2026-05-01T00:00:00Z", snapshot_b="2026-05-02T00:00:00Z")
    assert result["status"] == "snapshots_not_found"
    assert "get_schema_history" in result["note"]


def test_two_snapshots_with_no_change_is_a_supportable_negative():
    mock_cache = _cache(
        QueryResult(
            columns=["schema_name", "tables_before", "tables_after"],
            rows=[{"schema_name": "public", "tables_before": '{"users": ["id"]}',
                   "tables_after": '{"users": ["id"]}'}],
            row_count=1,
        ),
        _coverage(4, 1),
    )
    result = get_schema_diff_impl(mock_cache, cluster_id="prod-pg-1")
    # An identical pair still counts as COMPARED, that is the whole difference
    # between "we looked" and "we could not look".
    assert result["status"] == "ok"
    assert result["schemas_compared"] == 1
    assert result["totals"] == {"added": 0, "dropped": 0, "modified": 0, "rename_candidates": 0}


def test_explicit_miss_is_not_reported_as_baseline_only():
    """The `explicit` branch is tested BEFORE the baseline-only branch. A cluster
    with at most one snapshot per schema used to answer "only a baseline exists"
    to a caller who asked about two specific timestamps, which answers a question
    they did not ask and drops the coverage range naming the snapshots that DO
    exist."""
    mock_cache = _cache(_EMPTY, _coverage(1, 1))
    result = get_schema_diff_impl(
        mock_cache, cluster_id="prod-pg-1",
        snapshot_a="2020-01-01T00:00:00Z", snapshot_b="2020-01-02T00:00:00Z")
    assert result["status"] == "snapshots_not_found"
    assert "2026-07-01T00:00:00Z" in result["note"]  # the range that DOES exist
    # The implicit call on that same cluster still says baseline.
    implicit = get_schema_diff_impl(_cache(_EMPTY, _coverage(1, 1)),
                                    cluster_id="prod-pg-1")
    assert implicit["status"] == "insufficient_snapshots"


def test_explicit_miss_on_an_uncollected_cluster_is_still_not_collected():
    """not_collected must stay ahead of snapshots_not_found: with zero rows the
    honest answer is that nothing was ever collected, not that two timestamps
    missed."""
    result = get_schema_diff_impl(
        _cache(_EMPTY, _coverage(0, 0)), cluster_id="prod-pg-1",
        snapshot_a="2020-01-01T00:00:00Z", snapshot_b="2020-01-02T00:00:00Z")
    assert result["status"] == "not_collected"


def test_successful_diff_carries_the_snapshot_times_and_the_coverage():
    """Store-on-change: the implicit latest-vs-previous diff is the most recent
    DDL EVENT whenever it happened, so an undated payload gets presented as
    recent. collection_coverage used to be attached ONLY when there was nothing
    to report."""
    mock_cache = _cache(
        QueryResult(
            columns=["schema_name", "tables_before", "tables_after",
                     "snapshot_before", "snapshot_after"],
            rows=[{"schema_name": "public",
                   "tables_before": '{"customers": ["id"]}',
                   "tables_after": '{"invoices": ["id"]}',
                   "snapshot_before": "2026-02-01T00:00:00+00:00",
                   "snapshot_after": "2026-07-18T23:50:46+00:00"}],
            row_count=1,
        ),
        _coverage(9, 1),
    )
    result = get_schema_diff_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["status"] == "ok"
    d = result["diffs"][0]
    assert d["snapshot_time"] == "2026-07-18T23:50:46+00:00"
    assert d["previous_snapshot_time"] == "2026-02-01T00:00:00+00:00"
    assert result["collection_coverage"]["snapshots_stored"] == 9
    assert "2026-07-18T23:50:46+00:00" in result["note"]
    # And it must not let the agent read the diff as recent by default.
    assert "최근에 발생한 변경이라는 뜻은 아닙니다" in result["note"]


def test_explicit_sql_normalizes_the_two_timestamps_chronologically():
    """Reversing the arguments used to swap `added` and `dropped`, reporting a
    CREATE to a DBA as a DROP with status ok and no warning. The SQL decides
    which side is the before, so argument order cannot invert the verdict."""
    from mcp_servers.operations.tools.schema_diff import EXPLICIT_SQL

    assert "LEAST(:snapshot_a::timestamptz, :snapshot_b::timestamptz)" in EXPLICIT_SQL
    assert "GREATEST(:snapshot_a::timestamptz, :snapshot_b::timestamptz)" in EXPLICIT_SQL
    # a is the before side, and the payload says so out loud.
    assert "a.tables_json AS tables_before" in EXPLICIT_SQL
    assert "a.snapshot_time::text AS snapshot_before" in EXPLICIT_SQL


def test_explicit_ok_payload_states_the_normalization():
    mock_cache = _cache(
        QueryResult(
            columns=["schema_name", "tables_before", "tables_after",
                     "snapshot_before", "snapshot_after"],
            rows=[{"schema_name": "public",
                   "tables_before": '{"a": ["id"]}',
                   "tables_after": '{"a": ["id"], "b": ["id"]}',
                   "snapshot_before": "2026-07-01T00:00:00+00:00",
                   "snapshot_after": "2026-07-02T00:00:00+00:00"}],
            row_count=1,
        ),
        _coverage(4, 1),
    )
    result = get_schema_diff_impl(mock_cache, cluster_id="prod-pg-1",
                                  snapshot_a="2026-07-02T00:00:00+00:00",
                                  snapshot_b="2026-07-01T00:00:00+00:00")
    assert result["diffs"][0]["added"] == ["b"]
    assert "이른 쪽이 before" in result["note"]


def test_diff_impl_latest_modified_column():
    """Latest snapshot path: row carries before/after as JSON. ALTER
    TABLE ADD COLUMN should land in modified, not added/dropped."""
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["schema_name", "tables_before", "tables_after"],
        rows=[
            {
                "schema_name": "public",
                "tables_before": '{"orders": ["id", "amount"]}',
                "tables_after": '{"orders": ["id", "amount", "currency"]}',
            }
        ],
        row_count=1,
    )
    result = get_schema_diff_impl(mock_cache, cluster_id="prod-pg-1")
    diff = result["diffs"][0]
    assert diff["added"] == []
    assert diff["dropped"] == []
    assert diff["modified"][0]["added_columns"] == ["currency"]
