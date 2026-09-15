"""query_stats.query_text must not carry SQL Server user data.

The column has three producers and only one of them is raw:

  PostgreSQL   stats_collector.py     pg_stat_statements.query   constants are $N
  MySQL        mysql_query_stats.py   DIGEST_TEXT                constants are ?
  SQL Server   mssql_query_stats.py   dm_exec_sql_text().text    RAW STATEMENT

So literals reach the cache on SQL Server only, and five UI components render
that column verbatim to authenticated users (dashboard queries panel, query
detail modal, long-running panel, dashboard page, rca-candidate-detail).
Redacting in one reader would leave the other four exposed, so the scrub lives
at the producer and this file pins it there.

WHAT IS PINNED. Two halves, and the second is the one that matters: the unit
tests fix the scrubber's behaviour, and test_collector_scrubs_before_writing
proves collect_mssql_query_stats actually CALLS it, because a correct scrubber
that no row passes through protects nothing.
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


qs = _load("mssql_query_stats", "mssql_query_stats.py")
scrub = qs.scrub_sql_literals


def test_string_literal_with_escaped_quote_is_one_placeholder():
    """'' inside a literal is DATA, not the end of it. A scanner that treats the
    first quote as a terminator resumes in "SQL mode" inside the value and emits
    the rest of the name as bare text, which is the leak this test exists for."""
    sql = "SELECT id FROM Customers WHERE last_name = 'O''Brien' AND city = 'Cork'"
    assert scrub(sql) == (
        "SELECT id FROM Customers WHERE last_name = '?' AND city = '?'")
    assert "Brien" not in scrub(sql)


def test_numeric_literal_scrubbed_but_digits_in_identifiers_survive():
    """`table1`, `a1b2` and `@p1` all contain digits that are part of a NAME.
    Scrubbing those would destroy the statement's readability for the DBA, which
    is the whole reason we normalize rather than drop the column."""
    sql = "SELECT a1b2 FROM table1 WHERE qty > 500 AND rate = 1.75"
    assert scrub(sql) == (
        "SELECT a1b2 FROM table1 WHERE qty > ? AND rate = ?")


def test_already_parameterised_statement_is_unchanged():
    """sp_executesql traffic arrives with @p1 markers and NO literals. There is
    nothing to scrub, so the text must come out byte-identical."""
    sql = ("SELECT o.total FROM dbo.Orders o "
           "WHERE o.customer_id = @p1 AND o.placed_at >= @p2 ORDER BY o.id DESC")
    assert scrub(sql) == sql


def test_statement_with_no_literals_is_unchanged():
    sql = "SELECT COUNT(*) FROM dbo.Orders o JOIN dbo.Customers c ON c.id = o.customer_id"
    assert scrub(sql) == sql


def test_bracketed_and_quoted_identifiers_keep_their_contents():
    """[Order 2024] is a NAME containing a digit run with a space in front of it,
    which is exactly the shape the numeric rule fires on. Bracketed and quoted
    identifiers are passed through whole so a name cannot be mangled."""
    sql = 'SELECT [Qty 2024], "col 9" FROM [dbo].[Order 2024] WHERE [Qty 2024] > 7'
    assert scrub(sql) == (
        'SELECT [Qty 2024], "col 9" FROM [dbo].[Order 2024] WHERE [Qty 2024] > ?')


def test_apostrophe_in_a_comment_does_not_swallow_the_next_literal():
    """Why this is one left-to-right pass and not two regex replaces. The `'` in
    `don't` opens nothing (it is inside a comment), but a literal-first regex
    pairs it with the opening quote of 'Alice' and replaces the span BETWEEN
    them, leaving `Alice` standing as bare text. MEASURED: that ordering leaks."""
    sql = "SELECT name -- don't trust this\nFROM Users WHERE name = 'Alice'"
    out = scrub(sql)
    assert out == "SELECT name -- don't trust this\nFROM Users WHERE name = '?'"
    assert "Alice" not in out


def test_digits_and_quotes_inside_a_comment_are_left_alone():
    """The `/* source=... */` marker the platform relies on for audit must
    survive, so comment bodies are passed through verbatim."""
    sql = "/* source=dbops-agent v2 */ SELECT 1"
    assert scrub(sql) == "/* source=dbops-agent v2 */ SELECT ?"


def test_truncated_literal_is_still_replaced():
    """The DMV read is SUBSTRING(st.text, offset, 500), so a long statement
    arrives cut, frequently mid-literal. An unterminated literal is consumed to
    end of text: no closing quote must NOT mean no scrub."""
    sql = "SELECT * FROM Users WHERE email = 'alice@example.c"
    out = scrub(sql)
    assert out == "SELECT * FROM Users WHERE email = '?'"
    assert "example" not in out


def test_hex_and_exponent_forms_are_scrubbed():
    sql = "SELECT * FROM t WHERE h = 0xDEADBEEF AND f = 1.5e-3"
    assert scrub(sql) == "SELECT * FROM t WHERE h = ? AND f = ?"


def test_shape_agrees_with_the_other_two_engines():
    """The point of the exercise: the same statement read on all three engines
    should read alike. DIGEST_TEXT emits `?` for numbers and `?` for strings;
    we emit `'?'` for strings so the DBA can still see it WAS a string, which
    is what pg_stat_statements' `$1` also preserves by position."""
    mssql_raw = "SELECT c.name FROM Customers c WHERE c.tier = 'gold' AND c.age > 30"
    assert scrub(mssql_raw) == (
        "SELECT c.name FROM Customers c WHERE c.tier = '?' AND c.age > ?")


# --- the half that proves the scrubber is actually wired in -------------------


def _f(v):
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"longValue": v}


class _Fake:
    def __init__(self, query_text):
        self.query_text = query_text
        self.writes = []

    def execute_statement(self, **kw):
        assert "dm_exec_query_stats" in kw["sql"], kw["sql"]
        return {"records": [[
            _f("A1B2C3"), _f(self.query_text), _f(42),
            _f(1234.5), _f(29.4), _f(84),
        ]]}

    def cache_execute(self, sql, params):
        assert "INSERT INTO query_stats" in sql, sql
        self.writes.append(params)


def test_collector_scrubs_before_writing():
    """The row handed to cache_execute is what lands in the cache and what the
    five readers render. Asserting on scrub_sql_literals alone would still pass
    if this call site were removed, so assert on the WRITE."""
    raw = ("SELECT id FROM dbo.Orders1 WHERE customer_email = 'bob''s@corp.com' "
           "AND amount > 250")
    fake = _Fake(raw)

    result = qs.collect_mssql_query_stats(
        fake, fake.cache_execute, "", "", "dbops-demo-mssql", "master")

    assert result == {"cluster_id": "dbops-demo-mssql", "queries_collected": 1}
    written = fake.writes[0]["query_text"]
    assert written == (
        "SELECT id FROM dbo.Orders1 WHERE customer_email = '?' AND amount > ?")
    assert "bob" not in written
    assert "corp.com" not in written
