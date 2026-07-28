"""get_schema_history: the honest-empty-state contract.

The old `test_schema_history_no_changes` asserted `count == 0` for an empty
result and nothing else, which pinned the defect in place: an untouched schema
and a cluster that was never snapshotted produced the identical answer, and a
DBA asking "did anyone change the schema before the incident?" acts on those two
in opposite directions. These tests assert the DISTINCTION, so reverting the
reader to a bare `count: 0` fails here.

Real-SQL coverage (these queries actually parsing and returning these rows
against a live PostgreSQL server) lives in
tests/unit/data_pipeline/test_schema_snapshot_real_pg.py.
"""

from unittest.mock import MagicMock

from mcp_servers.operations.tools.schema_history import get_schema_history_impl
from mcp_servers.shared.models import QueryResult


def _cache(*results):
    """Cache whose execute() returns each QueryResult in turn: the reader issues
    the changes query first, then the coverage probe only if it came back empty."""
    mock = MagicMock()
    mock.execute.side_effect = list(results)
    return mock


_EMPTY = QueryResult(columns=[], rows=[], row_count=0)


def _coverage(snapshots, schemas, first="2026-07-01T00:00:00Z", last="2026-07-09T00:00:00Z"):
    return QueryResult(
        columns=["snapshots", "schemas", "first_seen", "last_seen"],
        rows=[{"snapshots": snapshots, "schemas": schemas,
               "first_seen": first if snapshots else None,
               "last_seen": last if snapshots else None}],
        row_count=1,
    )


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
    # The happy path must not pay for the coverage probe.
    assert mock_cache.execute.call_count == 1


def test_never_collected_is_not_reported_as_no_changes():
    """THE defect this tier removes: zero snapshots must not read as a clean
    "the schema never changed"."""
    mock_cache = _cache(_EMPTY, _coverage(0, 0))
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["status"] == "not_collected"
    assert result["count"] == 0
    assert result["period_days"] == 30
    assert result["collection_coverage"]["snapshots_stored"] == 0
    # The copy must state the absence of DATA, never the absence of CHANGE.
    assert "수집되지 않" in result["note"]
    assert "변경되지 않았다는 뜻이 아니" in result["note"]


def test_single_baseline_is_not_a_history():
    mock_cache = _cache(_EMPTY, _coverage(1, 1))
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["status"] == "baseline_only"
    assert "baseline" in result["note"]


def test_one_baseline_per_schema_is_still_baseline_only():
    """3 schemas x 1 baseline = 3 rows and still nothing comparable. A naive
    `snapshots == 1` check calls this "no changes"."""
    mock_cache = _cache(_EMPTY, _coverage(3, 3))
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["status"] == "baseline_only"


def test_real_negative_reports_the_coverage_that_supports_it():
    """Two+ snapshots on one schema and no change in the window IS a supportable
    negative, and it must be distinguishable from the two states above."""
    mock_cache = _cache(_EMPTY, _coverage(4, 2))
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1", days=7)
    assert result["status"] == "no_changes"
    assert result["collection_coverage"]["snapshots_stored"] == 4
    assert result["collection_coverage"]["first_snapshot"] == "2026-07-01T00:00:00Z"
    # The negative is stated WITH its evidence window, not bare.
    assert "2026-07-01T00:00:00Z" in result["note"]
