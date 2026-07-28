"""Unit tests for the structured schema_diff tool.

Covers the four-bucket output (added / dropped / modified /
rename_candidates) plus edge cases the heuristic has to handle:
column-order equivalence, string-encoded blob inputs, and missing
snapshots.
"""

from unittest.mock import MagicMock

from mcp_servers.operations.tools.schema_diff import (
    _parse_tables,
    get_schema_diff_impl,
)
from mcp_servers.shared.models import QueryResult

# compute_diff is NOT re-exported by the tool any more: no consumer may call it
# (see mcp_servers/shared/schema_diff_util.py), so a test that wants the raw
# four-bucket computation takes it from the contract module directly.
from mcp_servers.shared.schema_diff_util import compute_diff as _compute_diff

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
    mock_cache = _cache(QueryResult(
        columns=["schema_name", "tables_before", "tables_after"],
        rows=[
            {
                "schema_name": "public",
                "tables_before": '{"users": ["id"], "deleted_table": ["k"]}',
                "tables_after": '{"users": ["id"]}',
            }
        ],
        row_count=1,
    ), _coverage(4, 1))
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


_SCOPE = "dbops/16384"
_LAST_CONFIRMED = "2026-07-09T00:00:00Z"


def _observation(schemas=(("public", "y", "y"),), last=_LAST_CONFIRMED, age=60,
                 scope=_SCOPE):
    """OBSERVED_SQL's shape: one row per schema (its LATEST snapshot), carrying that
    schema's OWN read_scope and its OWN last_seen_at age.

    `schemas` entries are (name, holds_tables, confirmed), where confirmed "y"
    means the row is under the cluster's established scope with a fresh
    last_seen_at. "n" ages that schema's OWN stamp past the bar, which is the whole
    of the sixth pass's FINDING 3: the previous shape asked "is this schema the most
    recently seen one in the cluster", a RELATIVE test that reported zero
    unconfirmed schemas whenever every schema shared one stamp, however old.
    """
    return QueryResult(
        columns=["schema_name", "read_scope", "last_seen", "holds_tables", "age_sec"],
        rows=[{"schema_name": n, "read_scope": scope,
               "last_seen": last, "holds_tables": h,
               "age_sec": age if c == "y" else 40 * 24 * 3600}
              for n, h, c in schemas],
        row_count=len(schemas),
    )


def _scope_row(scope=_SCOPE):
    """ESTABLISHED_SCOPE_SQL's shape: the newest row that carries a scope, or none
    at all when every stored row predates schema_v27."""
    if scope is None:
        return QueryResult(columns=["read_scope"], rows=[], row_count=0)
    return QueryResult(columns=["read_scope"], rows=[{"read_scope": scope}],
                       row_count=1)


def _cache(*results, observation=None, scope=_SCOPE):
    """A cache that DISPATCHES ON SQL, not on call order.

    It used to be a positional side_effect list, and the order of statements is
    exactly what this tier changes: the scope and the per-schema observation have to
    be selected BEFORE a pair can be, because a pair that is not scope-filtered is
    the phantom mass DROP. A positional mock turns any such change into dozens of
    unrelated red tests and, worse, can hand the pair query the observation's rows.
    """
    pair, coverage = (list(results) + [_EMPTY, _EMPTY])[:2]
    obs = observation or _observation()

    def execute(sql, params=None):
        if "read_scope IS NOT NULL" in sql:
            return _scope_row(scope)
        if "holds_tables" in sql:
            return obs
        if "COUNT(*) AS snapshots" in sql:
            return coverage
        return pair
    mock = MagicMock()
    mock.execute.side_effect = execute
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
    mock_cache = _cache(QueryResult(
        columns=["schema_name", "tables_before", "tables_after"],
        rows=[
            {
                "schema_name": "public",
                "tables_before": '{"orders": ["id", "amount"]}',
                "tables_after": '{"orders": ["id", "amount", "currency"]}',
            }
        ],
        row_count=1,
    ), _coverage(4, 1))
    result = get_schema_diff_impl(mock_cache, cluster_id="prod-pg-1")
    diff = result["diffs"][0]
    assert diff["added"] == []
    assert diff["dropped"] == []
    assert diff["modified"][0]["added_columns"] == ["currency"]


# ---------------------------------------------------------------------------
# WHAT WAS NOT LOOKED AT. The producer no longer resolves "absent from my catalog
# read" to a DROP (it produced a phantom mass drop; see
# data-pipeline/etl_collector/collectors/schema_snapshot.py), so a schema nobody
# can see files no row at all. Without the observation probe that schema falls in
# with the unchanged ones and this tool reports it as an unchanged schema, which
# is the same false negative pointing the other way.
# ---------------------------------------------------------------------------


def _identical_pair(schema="public"):
    return QueryResult(
        columns=["schema_name", "tables_before", "tables_after"],
        rows=[{"schema_name": schema, "tables_before": '{"users": ["id"]}',
               "tables_after": '{"users": ["id"]}'}],
        row_count=1,
    )


def test_a_fully_confirmed_cluster_still_gets_the_clean_negative():
    result = get_schema_diff_impl(
        _cache(_EMPTY, _coverage(4, 1)), cluster_id="prod-pg-1")
    assert result["status"] == "no_changes"
    assert result["observation"]["status"] == "fresh"
    assert result["observation"]["unconfirmed_schemas"] == []
    # The negative names the confirmation that licenses it to cover the cluster.
    assert _LAST_CONFIRMED in result["note"]


def test_an_unconfirmed_schema_downgrades_no_changes_to_partial():
    """`core` still has stored tables and the newest read did not name it. Neither
    "dropped" nor "no changes" is supportable, so the answer is `partial` and the
    schema is NAMED as an unknown."""
    result = get_schema_diff_impl(
        _cache(_EMPTY, _coverage(6, 2),
               observation=_observation((("public", "y", "y"), ("core", "y", "n")))),
        cluster_id="prod-pg-1")
    assert result["status"] == "partial"
    assert result["observation"]["unconfirmed_schemas"] == ["core"]
    assert "core" in result["note"]
    assert "확인 불가" in result["note"]
    assert "삭제로 단정하지 않고" in result["note"]


def test_a_cycle_that_confirmed_nothing_names_every_schema_it_left_behind():
    """FINDING 3 of the sixth pass, at the point it is decided.

    A scope change or a stopped collector confirms NOTHING, so every schema's
    last_seen_at is equally old and no single schema stands out. The previous shape
    derived `confirmed_now` from the CLUSTER-WIDE MAX(last_seen_at), a RELATIVE
    test: every schema equalled that max, so `unconfirmed_schemas` came back EMPTY
    while the collector's own return value for the same cycle said two schemas were
    unconfirmed. MEASURED on PostgreSQL 14.18 pre-fix, one frozen cycle over a
    2-schema cluster: collector {"not_seen": 2, "not_seen_schemas":
    ["alpha", "public"]} against readers {"status": "fresh",
    "unconfirmed_schemas": []}.

    Each schema's OWN age against an ABSOLUTE bar is the fix, and the point of the
    fix is that the schemas are NAMED.
    """
    result = get_schema_diff_impl(
        _cache(_EMPTY, _coverage(6, 2),
               observation=_observation((("public", "y", "n"), ("core", "y", "n")),
                                        age=9 * 3600)),
        cluster_id="prod-pg-1")
    assert result["status"] == "partial"
    assert result["observation"]["status"] == "not_seen"
    assert result["observation"]["unconfirmed_schemas"] == ["core", "public"]
    for name in ("core", "public"):
        assert name in result["note"], result["note"]
    assert "확인되지 않았습니다" in result["note"]


def test_a_schema_whose_stored_map_is_already_empty_is_not_reported_unconfirmed():
    """A schema recorded as holding no tables is not serving anything to anybody,
    so its absence from a read claims nothing and must not raise an unknown on
    every one of the 288 daily runs forever."""
    result = get_schema_diff_impl(
        _cache(_EMPTY, _coverage(6, 2),
               observation=_observation((("public", "y", "y"), ("emptied", "n", "n")))),
        cluster_id="prod-pg-1")
    assert result["status"] == "no_changes"
    assert result["observation"]["unconfirmed_schemas"] == []


def test_an_unconfirmed_schema_is_reported_alongside_real_diffs_too():
    """An unknown does not disappear because some OTHER schema changed."""
    result = get_schema_diff_impl(
        _cache(QueryResult(
            columns=["schema_name", "tables_before", "tables_after"],
            rows=[{"schema_name": "public", "tables_before": '{"a": ["id"]}',
                   "tables_after": '{"a": ["id"], "b": ["id"]}'}],
            row_count=1),
            _coverage(6, 2),
            observation=_observation((("public", "y", "y"), ("core", "y", "n")))),
        cluster_id="prod-pg-1")
    assert result["status"] == "ok"
    assert result["observation"]["unconfirmed_schemas"] == ["core"]
    assert "core" in result["note"]


def test_a_dropped_list_carries_the_catalog_visibility_caveat():
    result = get_schema_diff_impl(
        _cache(QueryResult(
            columns=["schema_name", "tables_before", "tables_after"],
            rows=[{"schema_name": "public", "tables_before": '{"a": ["id"], "b": ["id"]}',
                   "tables_after": '{"a": ["id"]}'}],
            row_count=1), _coverage(6, 1)),
        cluster_id="prod-pg-1")
    assert result["totals"]["dropped"] == 1
    assert "권한 회수(REVOKE)" in result["note"]
    # And a payload with nothing dropped does not carry it.
    clean = get_schema_diff_impl(_cache(_identical_pair(), _coverage(4, 1)),
                                cluster_id="prod-pg-1")
    assert clean["totals"]["dropped"] == 0
    assert "권한 회수(REVOKE)" not in clean["note"]


def test_a_cache_without_the_migration_says_so_instead_of_claiming_no_changes():
    """schema_v27 not applied yet: the probe raises, which is not a licence to
    answer "no changes" for the whole cluster. No exception text may reach the
    payload (the AST leak guard's allowlist is empty and stays empty).

    And with no scope there is NO PAIR QUERY AT ALL. That is the contract, not an
    optimisation: comparing two blobs whose catalog is unknown is the phantom mass
    DROP, so `compare()` raises rather than falling back and the reader does not
    reach it.
    """
    def execute(sql, params=None):
        if "read_scope IS NOT NULL" in sql or "holds_tables" in sql:
            raise RuntimeError("column last_seen_at does not exist")
        if "COUNT(*) AS snapshots" in sql:
            return _coverage(6, 2)
        raise AssertionError("a pair must not be selected without a scope: " + sql)
    mock = MagicMock()
    mock.execute.side_effect = execute
    result = get_schema_diff_impl(mock, cluster_id="prod-pg-1")
    # `not_comparable`, NOT `partial`: partial claims some of the question was
    # answered with a real negative, and here NOTHING was compared, because
    # comparability could not even be established. WHY it could not is the
    # observation's own status, which is named and never swallowed.
    assert result["status"] == "not_comparable"
    assert result["observation"]["status"] == "unavailable"
    assert result["schemas_compared"] == 0
    assert "last_seen_at" not in result["note"]
    assert "schema_v27" in result["note"]


def test_history_with_no_scope_at_all_is_not_comparable_and_not_no_changes():
    """Every stored row predates schema_v27, so no row says which catalog it
    describes. The previous pass answered this cluster "only a baseline exists",
    which is not what it was asked, and its blob-diff readers compared the rows
    anyway. There is now a status for it and no pair is selected."""
    def execute(sql, params=None):
        if "read_scope IS NOT NULL" in sql:
            return _scope_row(None)
        if "holds_tables" in sql:
            return _observation((("public", "y", "y"),), scope=None)
        if "COUNT(*) AS snapshots" in sql:
            return _coverage(6, 2)
        raise AssertionError("a pair must not be selected without a scope: " + sql)
    mock = MagicMock()
    mock.execute.side_effect = execute
    result = get_schema_diff_impl(mock, cluster_id="prod-pg-1")
    assert result["status"] == "not_comparable"
    assert result["observation"]["status"] == "unmigrated"
    assert result["schemas_compared"] == 0
    assert "schema_v27" in result["note"]
