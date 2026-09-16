"""The OTHER three SQL Server statement columns must not carry user data.

test_mssql_query_stats_scrub.py pinned the scrub on query_stats.query_text.
mssql_activity.py reads the SAME raw source, sys.dm_exec_sql_text(), into three
more columns, and it was left unscrubbed:

  long_running_queries.query_text     dm_exec_requests     RAW STATEMENT
  blocking_locks.blocked_query        dm_exec_requests     RAW STATEMENT
  blocking_locks.blocking_query       dm_exec_requests     RAW STATEMENT

Fixing one producer and not its sibling is worse than fixing neither, because
the first fix is what makes the column look safe. Both tables are rendered
verbatim to authenticated users (the long-running panel, the blocking panel and
RCA candidate evidence), so all three are scrubbed at the producer.

Every assertion here is on the row handed to cache_execute, not on the scrubber:
a correct scrubber that no row passes through protects nothing, and asserting on
scrub_sql_literals alone would still pass with all three call sites deleted.
"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "rds_direct_collector"


def _load(name, rel):
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(name, _ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


act = _load("mssql_activity", "mssql_activity.py")


def _f(v):
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"longValue": v}


# Literals a DBA must never read off the dashboard, one per column so a fix
# applied to only one of the three cannot pass.
LONG_RAW = "SELECT id FROM dbo.Orders1 WHERE customer_email = 'bob''s@corp.com' AND amount > 250"
BLOCKED_RAW = "UPDATE Patients SET ssn = '123-45-6789' WHERE mrn = 'MRN0042'"
BLOCKING_RAW = "DELETE FROM Cards WHERE pan = '4111111111111111'"


class _Fake:
    """One row from each of the three DMV reads, routed by the SQL it is given.

    The router ASSERTS on an unrecognized statement rather than returning an
    empty result: a collector whose SQL changed shape would otherwise write
    nothing and every assertion below would vacuously pass.
    """

    def __init__(self):
        self.writes = {}

    def execute_statement(self, **kw):
        sql = kw["sql"]
        if "dm_exec_sessions s" in sql and "GROUP BY s.status" in sql:
            return {"records": [[_f("running"), _f(3)]]}
        if "r.total_elapsed_time > 5000" in sql:
            return {"records": [[
                _f(57), _f("app_user"), _f("running"), _f(12.5),
                _f(LONG_RAW), _f("PAGEIOLATCH_SH"), _f("10.0.0.7"),
            ]]}
        if "r.blocking_session_id <> 0" in sql:
            return {"records": [[
                _f(57), _f("app_user"), _f(61), _f("batch_user"),
                _f(BLOCKED_RAW), _f(BLOCKING_RAW),
                _f("LCK_M_X"), _f("KEY: 7:12345"), _f(4.0),
            ]]}
        raise AssertionError(f"unrouted DMV read: {sql}")

    def cache_execute(self, sql, params):
        for table in ("metric_snapshots", "long_running_queries", "blocking_locks"):
            if f"INSERT INTO {table}" in sql:
                self.writes.setdefault(table, []).append(params)
                return
        raise AssertionError(f"unexpected write: {sql}")


def _run():
    fake = _Fake()
    result = act.collect_mssql_activity(
        fake, fake.cache_execute, "", "", "dbops-demo-mssql", "master")
    return fake, result


def test_the_collector_actually_wrote_all_three_tables():
    """Guards every other test in this file: if the collector stopped writing,
    the leak assertions below would pass on an empty dict."""
    fake, result = _run()
    assert result == {
        "cluster_id": "dbops-demo-mssql",
        "activity_states": 1,
        "long_running": 1,
        "blocking_locks": 1,
    }
    assert len(fake.writes["long_running_queries"]) == 1
    assert len(fake.writes["blocking_locks"]) == 1


def test_long_running_query_text_is_scrubbed_before_the_write():
    fake, _ = _run()
    written = fake.writes["long_running_queries"][0]["query_text"]
    assert written == (
        "SELECT id FROM dbo.Orders1 WHERE customer_email = '?' AND amount > ?")
    assert "bob" not in written
    assert "corp.com" not in written


def test_both_blocking_columns_are_scrubbed_before_the_write():
    """Two columns from two separate OUTER APPLYs, so scrubbing one and not the
    other is a live possibility this pins shut."""
    fake, _ = _run()
    row = fake.writes["blocking_locks"][0]
    assert row["blocked_query"] == (
        "UPDATE Patients SET ssn = '?' WHERE mrn = '?'")
    assert row["blocking_query"] == "DELETE FROM Cards WHERE pan = '?'"
    assert "123-45-6789" not in row["blocked_query"]
    assert "MRN0042" not in row["blocked_query"]
    assert "4111111111111111" not in row["blocking_query"]


def test_no_literal_from_any_column_survives_anywhere_in_the_written_rows():
    """The backstop, in case a fourth statement column is added later: no raw
    literal from any of the three sources may appear in ANY value written."""
    fake, _ = _run()
    blob = repr(fake.writes)
    for secret in ("bob", "corp.com", "123-45-6789", "MRN0042", "4111111111111111"):
        assert secret not in blob, secret


def test_the_identifiers_a_dba_needs_survive_the_scrub():
    """The scrub normalizes, it does not drop the column. A redaction that also
    removed the table and parameter names would make the panel useless, which
    is why this is a scrubber and not a DELETE."""
    fake, _ = _run()
    long_text = fake.writes["long_running_queries"][0]["query_text"]
    row = fake.writes["blocking_locks"][0]
    assert "dbo.Orders1" in long_text and "customer_email" in long_text
    assert "Patients" in row["blocked_query"] and "ssn" in row["blocked_query"]
    assert "Cards" in row["blocking_query"] and "pan" in row["blocking_query"]
    # Non-statement columns are untouched: they are not user data.
    assert row["relation"] == "KEY: 7:12345"
    assert fake.writes["long_running_queries"][0]["client_addr"] == "10.0.0.7"
