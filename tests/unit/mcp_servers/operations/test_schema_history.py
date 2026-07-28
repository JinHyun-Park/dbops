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


def _cache(*results):
    """Cache whose execute() returns each QueryResult in turn. Call order is:
    the changes query, the observation probe, then the coverage probe only if the
    changes query came back empty."""
    mock = MagicMock()
    mock.execute.side_effect = list(results)
    return mock


_EMPTY = QueryResult(columns=[], rows=[], row_count=0)
_LAST = "2026-07-09T00:00:00Z"


def _coverage(snapshots, schemas, first="2026-07-01T00:00:00Z", last=_LAST):
    return QueryResult(
        columns=["snapshots", "schemas", "first_seen", "last_seen"],
        rows=[{"snapshots": snapshots, "schemas": schemas,
               "first_seen": first if snapshots else None,
               "last_seen": last if snapshots else None}],
        row_count=1,
    )


def _observation(schemas=(("public", "y", "y"),), last=_LAST, age=60):
    """OBSERVATION_SQL's shape: one row per schema (latest snapshot), plus the
    cluster-wide newest observation repeated on every row.
    `schemas` is (name, holds_tables, confirmed_in_the_newest_read)."""
    return QueryResult(
        columns=["schema_name", "last_seen", "holds_tables", "confirmed_now",
                 "last_confirmed", "age_sec"],
        rows=[{"schema_name": n, "last_seen": last if c == "y" else None,
               "holds_tables": h, "confirmed_now": c,
               "last_confirmed": last, "age_sec": age}
              for n, h, c in schemas],
        row_count=len(schemas),
    )


def test_schema_history_with_changes():
    mock_cache = _cache(QueryResult(
        columns=["snapshot_time", "schema_name", "changes"],
        rows=[
            {"snapshot_time": "2024-01-02T00:00:00Z", "schema_name": "public", "changes": '{"added": ["orders"]}'},
            {"snapshot_time": "2024-01-01T00:00:00Z", "schema_name": "public", "changes": '{"added": ["users"]}'},
        ],
        row_count=2,
    ), _observation())
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1", days=7)
    assert result["status"] == "ok"
    assert result["cluster_id"] == "prod-pg-1"
    assert result["period_days"] == 7
    assert result["count"] == 2
    assert len(result["changes"]) == 2
    # The happy path pays for the observation probe and NOT for the coverage
    # probe: an unconfirmed schema matters even when other schemas did change.
    assert mock_cache.execute.call_count == 2
    assert result["observation"]["status"] == "fresh"
    # A dropped list is only as good as the catalog the collector could read, and
    # on MySQL that catalog is privilege-filtered. Disclosed, not resolved.
    assert "권한 회수(REVOKE)" in result["note"]


def test_never_collected_is_not_reported_as_no_changes():
    """THE defect this tier removes: zero snapshots must not read as a clean
    "the schema never changed"."""
    mock_cache = _cache(_EMPTY, QueryResult(columns=[], rows=[], row_count=0),
                        _coverage(0, 0))
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
    mock_cache = _cache(_EMPTY, _observation(), _coverage(1, 1))
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["status"] == "baseline_only"
    assert "baseline" in result["note"]


def test_one_baseline_per_schema_is_still_baseline_only():
    """3 schemas x 1 baseline = 3 rows and still nothing comparable. A naive
    `snapshots == 1` check calls this "no changes"."""
    mock_cache = _cache(_EMPTY, _observation(), _coverage(3, 3))
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["status"] == "baseline_only"


def test_real_negative_reports_the_coverage_that_supports_it():
    """Two+ snapshots on one schema, no change in the window, AND every schema
    confirmed by the newest read: only then is the negative supportable."""
    mock_cache = _cache(_EMPTY, _observation(), _coverage(4, 2))
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
    mock_cache = _cache(_EMPTY,
                        _observation((("public", "y", "y"), ("core", "y", "n"))),
                        _coverage(6, 2))
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1", days=7)
    assert result["status"] == "partial"
    assert result["observation"]["unconfirmed_schemas"] == ["core"]
    assert "core" in result["note"]
    assert "확인 불가" in result["note"]
    assert "삭제로 단정하지 않고" in result["note"]


def test_a_cluster_nothing_has_confirmed_lately_is_also_partial():
    """No per-schema unknown, but nothing has been confirmed for hours: a stopped
    collector, or every read landing outside the scope the history came from.
    Either way "no changes" cannot be claimed for the window."""
    mock_cache = _cache(_EMPTY, _observation(age=9 * 3600), _coverage(6, 2))
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1", days=7)
    assert result["status"] == "partial"
    assert result["observation"]["status"] == "stale"
    assert "갱신되지 않았습니다" in result["note"]


def test_a_cache_without_the_migration_says_so_instead_of_claiming_no_changes():
    """schema_v27 not applied yet: the probe raises. That is not a licence to
    answer "no changes" for the whole cluster, and no exception text may reach the
    payload."""
    mock = MagicMock()
    mock.execute.side_effect = [_EMPTY, RuntimeError("column last_seen_at does not exist"),
                                _coverage(6, 2)]
    result = get_schema_history_impl(mock, cluster_id="prod-pg-1", days=7)
    assert result["status"] == "partial"
    assert result["observation"] == {"status": "unavailable"}
    assert "last_seen_at" not in result["note"]
    assert "schema_v27" in result["note"]
