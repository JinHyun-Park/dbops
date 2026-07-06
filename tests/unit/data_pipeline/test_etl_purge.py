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
