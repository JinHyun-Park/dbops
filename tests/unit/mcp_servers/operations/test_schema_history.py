"""get_schema_history: the honest-empty-state contract.

The old `test_schema_history_no_changes` asserted `count == 0` for an empty
result and nothing else, which pinned the defect in place: an untouched schema
and a cluster that was never snapshotted produced the identical answer, and a
DBA asking "did anyone change the schema before the incident?" acts on those two
in opposite directions. These tests assert the DISTINCTION, so reverting the
reader to a bare `count: 0` fails here.

A third state joins them here: a schema the collector can no longer SEE. It files
no row, so it used to sit inside the same empty result as a genuinely unchanged
schema. The producer no longer resolves that absence to a DROP (it produced a
phantom mass drop), so this reader has to carry the unknown.

Real-SQL coverage (these queries actually parsing and returning these rows
against a live PostgreSQL server) lives in
tests/unit/data_pipeline/test_schema_snapshot_real_pg.py.
"""

from unittest.mock import MagicMock

from mcp_servers.operations.tools.schema_history import get_schema_history_impl
from mcp_servers.shared.models import QueryResult

_LAST = "2026-07-09T00:00:00Z"
_SCOPE = "dbops/16384"
_EMPTY = QueryResult(columns=[], rows=[], row_count=0)


def _coverage(snapshots, schemas, first="2026-07-01T00:00:00Z", last=_LAST):
    return QueryResult(
        columns=["snapshots", "schemas", "first_seen", "last_seen"],
        rows=[{"snapshots": snapshots, "schemas": schemas,
               "first_seen": first if snapshots else None,
               "last_seen": last if snapshots else None}],
        row_count=1,
    )


def _observation(schemas=(("public", "y", "y"),), last=_LAST, age=60, scope=_SCOPE):
    """OBSERVED_SQL's shape: one row per schema (its LATEST snapshot), carrying that
    schema's OWN read_scope and its OWN last_seen_at age. `schemas` entries are
    (name, holds_tables, confirmed); "n" ages that schema's own stamp past the bar.
    """
    return QueryResult(
        columns=["schema_name", "read_scope", "last_seen", "holds_tables", "age_sec"],
        rows=[{"schema_name": n, "read_scope": scope, "last_seen": last,
               "holds_tables": h,
               "age_sec": age if c == "y" else 40 * 24 * 3600}
              for n, h, c in schemas],
        row_count=len(schemas),
    )


def _scope_row(scope=_SCOPE):
    if scope is None:
        return QueryResult(columns=["read_scope"], rows=[], row_count=0)
    return QueryResult(columns=["read_scope"], rows=[{"read_scope": scope}], row_count=1)


def _cache(changes=None, observation=None, coverage=None, raises=None,
           scope=_SCOPE):
    """A cache that DISPATCHES ON SQL, not on call order.

    It was a positional side_effect list, and the statement ORDER is exactly what
    the selection contract changes: the scope and the per-schema observation have to
    be selected before anything else, because an unscoped pair is the phantom mass
    DROP. A positional mock turns that into a wall of unrelated red and can hand one
    statement another's rows.
    """
    obs = observation if observation is not None else _observation()

    def execute(sql, params=None):
        if "read_scope IS NOT NULL" in sql or "holds_tables" in sql:
            if raises:
                raise raises
            return _scope_row(scope) if "read_scope IS NOT NULL" in sql else obs
        if "COUNT(*) AS snapshots" in sql:
            return coverage if coverage is not None else _coverage(0, 0)
        return changes if changes is not None else _EMPTY
    mock = MagicMock()
    mock.execute.side_effect = execute
    return mock


def test_schema_history_with_changes():
    mock_cache = _cache(QueryResult(
        columns=["snapshot_time", "schema_name", "changes"],
        rows=[
            {"snapshot_time": "2024-01-02T00:00:00Z", "schema_name": "public", "changes": '{"added": ["orders"]}'},
            {"snapshot_time": "2024-01-01T00:00:00Z", "schema_name": "public", "changes": '{"added": ["users"]}'},
        ],
        row_count=2,
    ))
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1", days=7)
    assert result["status"] == "ok"
    assert result["cluster_id"] == "prod-pg-1"
    assert result["period_days"] == 7
    assert result["count"] == 2
    assert len(result["changes"]) == 2
    # The happy path pays for the observation probes and NOT for the coverage
    # probe: an unconfirmed schema matters even when other schemas did change.
    # Three statements: the window read, the established scope, the per-schema
    # observation.
    assert mock_cache.execute.call_count == 3
    assert not any("COUNT(*) AS snapshots" in c.args[0]
                   for c in mock_cache.execute.call_args_list)
    assert result["observation"]["status"] == "fresh"
    # A dropped list is only as good as the catalog the collector could read, and
    # on MySQL that catalog is privilege-filtered. Disclosed, not resolved.
    assert "권한 회수(REVOKE)" in result["note"]


def test_never_collected_is_not_reported_as_no_changes():
    """THE defect this tier removes: zero snapshots must not read as a clean
    "the schema never changed"."""
    mock_cache = _cache(observation=QueryResult(columns=[], rows=[], row_count=0),
                        coverage=_coverage(0, 0))
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["status"] == "not_collected"
    assert result["count"] == 0
    assert result["period_days"] == 30
    assert result["collection_coverage"]["snapshots_stored"] == 0
    assert result["observation"]["status"] == "no_snapshots"
    # The copy must state the absence of DATA, never the absence of CHANGE.
    assert "수집되지 않" in result["note"]
    assert "변경되지 않았다는 뜻이 아니" in result["note"]


def test_single_baseline_is_not_a_history():
    mock_cache = _cache(coverage=_coverage(1, 1))
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["status"] == "baseline_only"
    assert "baseline" in result["note"]


def test_one_baseline_per_schema_is_still_baseline_only():
    """3 schemas x 1 baseline = 3 rows and still nothing comparable. A naive
    `snapshots == 1` check calls this "no changes"."""
    mock_cache = _cache(coverage=_coverage(3, 3))
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["status"] == "baseline_only"


def test_real_negative_reports_the_coverage_that_supports_it():
    """Two+ snapshots on one schema, no change in the window, AND every schema
    confirmed by the newest read: only then is the negative supportable."""
    mock_cache = _cache(coverage=_coverage(4, 2))
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1", days=7)
    assert result["status"] == "no_changes"
    assert result["collection_coverage"]["snapshots_stored"] == 4
    assert result["collection_coverage"]["first_snapshot"] == "2026-07-01T00:00:00Z"
    # The negative is stated WITH its evidence window, not bare.
    assert "2026-07-01T00:00:00Z" in result["note"]
    # ...and with the confirmation that licenses it to cover the whole cluster.
    assert _LAST in result["note"]


def test_an_unconfirmed_schema_downgrades_the_negative_to_partial():
    """The state the producer used to resolve to a DROP. `core` still has stored
    tables and the newest catalog read did not name it: neither "dropped" nor "no
    changes" is supportable, so the window result is `partial` and the schema is
    NAMED as an unknown."""
    mock_cache = _cache(
        observation=_observation((("public", "y", "y"), ("core", "y", "n"))),
        coverage=_coverage(6, 2))
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1", days=7)
    assert result["status"] == "partial"
    assert result["observation"]["unconfirmed_schemas"] == ["core"]
    assert "core" in result["note"]
    assert "확인 불가" in result["note"]
    assert "삭제로 단정하지 않고" in result["note"]


def test_a_cycle_that_confirmed_nothing_names_every_schema_it_left_behind():
    """FINDING 3 of the sixth pass. Nothing has been confirmed for hours: a stopped
    collector, or a read landing outside the scope the history came from. Every
    schema's last_seen_at is then equally old, so the previous CLUSTER-WIDE-MAX test
    ("is this schema the most recently seen one") reported zero unconfirmed schemas
    and status `stale`, which never named a single schema. Measured pre-fix against
    a frozen cycle: collector `not_seen 2 ["alpha","public"]`, readers
    `{"status": "fresh", "unconfirmed_schemas": []}`.

    Per-schema against an ABSOLUTE bar names them, which is the point."""
    mock_cache = _cache(
        observation=_observation((("public", "y", "n"), ("core", "y", "n")),
                                 age=9 * 3600),
        coverage=_coverage(6, 2))
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1", days=7)
    assert result["status"] == "partial"
    assert result["observation"]["status"] == "not_seen"
    assert result["observation"]["unconfirmed_schemas"] == ["core", "public"]
    for name in ("core", "public"):
        assert name in result["note"], result["note"]
    assert "확인되지 않았습니다" in result["note"]


def test_a_cache_without_the_migration_says_so_instead_of_claiming_no_changes():
    """schema_v27 not applied yet: the probe raises. That is not a licence to
    answer "no changes" for the whole cluster, and no exception text may reach the
    payload."""
    mock = _cache(coverage=_coverage(6, 2),
                  raises=RuntimeError("column last_seen_at does not exist"))
    result = get_schema_history_impl(mock, cluster_id="prod-pg-1", days=7)
    assert result["status"] == "partial"
    assert result["observation"]["status"] == "unavailable"
    assert "last_seen_at" not in result["note"]
    assert "schema_v27" in result["note"]


def test_history_with_no_scope_at_all_is_still_replayed_but_never_a_negative():
    """Every stored row predates schema_v27. The REPLAY still works, deliberately:
    a stored diff was computed against a same-scope predecessor by construction, so
    scope-filtering the replay would erase real DDL history from the record. What is
    NOT allowed is calling an empty window a negative, because nothing about this
    cluster is currently comparable."""
    mock = _cache(observation=_observation((("public", "y", "y"),), scope=None),
                  coverage=_coverage(6, 2), scope=None)
    result = get_schema_history_impl(mock, cluster_id="prod-pg-1", days=7)
    assert result["status"] == "partial"
    assert result["observation"]["status"] == "unmigrated"
    assert "schema_v27" in result["note"]
