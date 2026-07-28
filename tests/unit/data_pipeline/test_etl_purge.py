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


def test_the_exemption_is_scoped_so_an_orphan_can_age_out():
    """The third finding of the seventh pass, pinned in the SQL.

    The exemption used to be per (cluster, schema) with no notion of scope, so a
    schema that exists ONLY under a scope the collector no longer reads had exactly
    one row, was always its own MAX, and survived every purge forever. `observed()`
    then reported it as unconfirmed for the life of the cluster, which made
    `observation_is_complete` permanently False and pinned three consumers to
    `partial`. MEASURED on PostgreSQL 14.18 before this change: after aging every
    row 200 days and running this statement, `[['stray', 'wrongdb/16687']]`
    survived.

    Two things have to hold and the SECOND one is the whole bug:
      * the exemption is restricted to the ESTABLISHED scope, and
      * the comparison is IS DISTINCT FROM, because for the orphan the subquery is
        NULL and `snapshot_time <> NULL` is NULL, i.e. the row is KEPT.
    The surviving rows are asserted against a live server in
    test_schema_snapshot_real_pg.py; this is the shape guard beside it.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_purge_etl_handler_scoped",
        Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector" / "handler.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sql = mod.SCHEMA_SNAPSHOTS_PURGE_SQL
    assert "IS DISTINCT FROM" in sql, (
        "`<>` keeps every row whose subquery is NULL, which is exactly the orphan"
    )
    assert "s.snapshot_time <> (" not in sql
    assert "x.read_scope = (SELECT e.read_scope" in sql, (
        "the exemption is not restricted to the cluster's established scope, so the "
        "last row of an abandoned scope is exempt forever again"
    )
    assert "e.read_scope IS NOT NULL" in sql
    assert "ORDER BY e.snapshot_time DESC LIMIT 1" in sql
