"""Regression guard on the etl_collector retention purges.

metric_snapshots and query_stats both grow unbounded and are purged best-effort
at the tail of each collection run. There is no isolated function to unit-test
(the purge is inline in lambda_handler behind boto3/env), so we guard the SQL
text: the table, column, and 90-day interval must stay correct. Shortening the
query_stats window would silently break the 90d SLO latency SLI.

RUNNABLE FILE-ALONE, which is FINDING 3 of the ninth pass. The two
schema_snapshots guards used to `exec_module` the handler by path, and the
handler's first import is `from collectors.capacity_forecast import ...`, which
only resolves when data-pipeline/etl_collector is on sys.path. Nothing here put it
there: a SIBLING module (test_schema_snapshot_real_pg.py) did, at ITS import time,
so the two guards passed under `pytest tests/unit` and ERRORED under `pytest
tests/unit/data_pipeline/test_etl_purge.py`. MEASURED before this change: `2
failed, 3 passed`, ModuleNotFoundError: No module named 'collectors'. A guard that
cannot be run in isolation makes a mutation check on the retention decision return
a FALSE result, and the eighth pass's report relied on exactly such a check.

So the constant is read out of the SOURCE (`_purge_sql`) instead of imported. The
value is identical (verified against the imported constant: implicit string
concatenation is folded into one ast.Constant by the parser), and nothing here
needs the handler's boto3 clients or its forty collector imports. The statement is
EXECUTED against a live PostgreSQL in
tests/unit/data_pipeline/test_schema_snapshot_real_pg.py, which loads the module
properly because it puts collectors/ on sys.path itself.
"""

import ast
from pathlib import Path

_HANDLER_PATH = (
    Path(__file__).resolve().parents[3]
    / "data-pipeline"
    / "etl_collector"
    / "handler.py"
)
_HANDLER = _HANDLER_PATH.read_text()


def _purge_sql(name="SCHEMA_SNAPSHOTS_PURGE_SQL"):
    """One MODULE-LEVEL string constant of etl_collector/handler.py, from its AST.

    Module-level is part of the contract, not an implementation detail: the
    real-engine test reads the same constant off the imported module in order to run
    it, so a statement inlined back into lambda_handler would leave that test with
    nothing to execute. This raises rather than skipping in that case.
    """
    for node in ast.parse(_HANDLER).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(
        f"{name} is no longer a module-level constant of "
        f"{_HANDLER_PATH.name}, so neither this guard nor the real-engine test "
        "that EXECUTES it can reach the statement"
    )


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


def test_schema_snapshots_purge_never_takes_the_comparison_pair():
    """String guard on the shape; the RESULT is asserted against a live
    PostgreSQL server in test_schema_snapshot_real_pg.py, which is what actually
    proves the correlated subquery works. Both are needed: under store-on-change
    the CURRENT snapshot of a schema untouched for 90 days is itself past the
    cutoff, and deleting it destroys the only row the next diff can compare to.

    TWO rows, not one, and that is FINDING 3 of the eighth pass. The
    surviving row's stored diff is REPLAYED by the timeline, get_schema_history and
    diagnose_root_cause; the PAIR behind it is what get_schema_diff and the dashboard
    panel RECOMPUTE. Exempting only the newest row left the replay family reporting
    an event the recompute family called `baseline_only`, a status whose sentence
    claims only one snapshot was ever collected.
    """
    sql = _purge_sql()
    assert "DELETE FROM schema_snapshots s " in sql
    assert "s.snapshot_time < NOW() - INTERVAL '90 days'" in sql
    assert "ORDER BY x.snapshot_time DESC LIMIT 2" in sql, (
        "the exempt set is the comparison PAIR; LIMIT 1 leaves the last recorded "
        "change replayable but no longer recomputable"
    )
    assert "MAX(x.snapshot_time)" not in sql, (
        "a MAX is one row by construction, which is the shape this finding replaced"
    )
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
      * a row whose exempt set is EMPTY (which is exactly the orphan) is DELETED.
        The seventh pass got that from `IS DISTINCT FROM` because the scalar
        subquery returned NULL; the exempt set is now a SET, so NOT EXISTS gives it
        structurally. `snapshot_time NOT IN (<set>)` would bring the trap straight
        back: one NULL in the set makes the predicate NULL and keeps the row.
    The surviving rows are asserted against a live server in
    test_schema_snapshot_real_pg.py; this is the shape guard beside it.
    """
    sql = _purge_sql()
    assert "AND NOT EXISTS (" in sql, (
        "an empty exempt set must DELETE the row, which is what ages the orphan out"
    )
    for null_trap in ("s.snapshot_time <> (", "s.snapshot_time NOT IN ("):
        assert null_trap not in sql, null_trap
    assert "x.read_scope = (SELECT e.read_scope" in sql, (
        "the exemption is not restricted to the cluster's established scope, so the "
        "last row of an abandoned scope is exempt forever again"
    )
    assert "e.read_scope IS NOT NULL" in sql
    assert "ORDER BY e.snapshot_time DESC LIMIT 1" in sql
