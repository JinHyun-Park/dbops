"""Regression guard on the etl_collector retention purges.

metric_snapshots and query_stats both grow unbounded and are purged best-effort
at the tail of each collection run. There is no isolated function to unit-test
(the purge is inline in lambda_handler behind boto3/env), so we guard the SQL
text: the table, column, and 90-day interval must stay correct. Shortening the
query_stats window would silently break the 90d SLO latency SLI.
"""

from pathlib import Path

_HANDLER = (
    Path(__file__).resolve().parents[3]
    / "data-pipeline"
    / "etl_collector"
    / "handler.py"
).read_text()


def test_metric_snapshots_purge_sql_intact():
    assert "DELETE FROM metric_snapshots " in _HANDLER
    assert "WHERE ts < NOW() - INTERVAL '90 days'" in _HANDLER


def test_query_stats_purge_sql_intact():
    # column is snapshot_time (NOT ts), 90-day window, its own best-effort block.
    assert "DELETE FROM query_stats " in _HANDLER
    assert "WHERE snapshot_time < NOW() - INTERVAL '90 days'" in _HANDLER
    assert "[etl] query_stats purge failed" in _HANDLER


def test_schema_snapshots_purge_is_its_own_best_effort_block():
    """A purge failure must not break collection nor the two purges above."""
    assert "[etl] schema_snapshots purge failed" in _HANDLER
    assert "SCHEMA_SNAPSHOTS_PURGE_SQL" in _HANDLER


def test_schema_snapshots_purge_never_takes_the_latest_snapshot():
    """String guard on the shape; the RESULT is asserted against a live
    PostgreSQL server in test_schema_snapshot_real_pg.py, which is what actually
    proves the correlated subquery works. Both are needed: under store-on-change
    the CURRENT snapshot of a schema untouched for 90 days is itself past the
    cutoff, and deleting it destroys the only row the next diff can compare to."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_purge_etl_handler",
        Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector" / "handler.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sql = mod.SCHEMA_SNAPSHOTS_PURGE_SQL
    assert "DELETE FROM schema_snapshots s " in sql
    assert "s.snapshot_time < NOW() - INTERVAL '90 days'" in sql
    assert "MAX(x.snapshot_time)" in sql
    assert "x.cluster_id = s.cluster_id AND x.schema_name = s.schema_name" in sql
