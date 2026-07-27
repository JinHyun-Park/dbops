"""pg_health_checks catalog SQL must qualify columns that exist in both joined
relations.

Live regression (2026-07-24): the txid_age check joined pg_stat_user_tables to
pg_class and selected a BARE `relname`. Both relations have that column, so
PostgreSQL rejected the statement with

    ERROR: column reference "relname" is ambiguous

on EVERY 5-minute ETL cycle. The per-check try/except printed the error to the
Lambda log, where it sat unread, so transaction-ID wraparound monitoring, the
single most consequential vacuum check on the flagship engine, silently produced
nothing. It only surfaced after the Aurora PostgreSQL log export was enabled and
the DB's own log showed DBOps' query failing.

A unit test cannot ask PostgreSQL to parse the SQL, so it checks the property
that actually broke: in a statement that joins two catalog relations, the
columns those relations share must carry a table alias."""

import importlib.util
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SRC = _ROOT / "data-pipeline" / "etl_collector" / "collectors" / "pg_health_checks.py"

# Columns that appear in MORE THAN ONE pg_catalog / pg_stat_* relation, so a
# bare reference is ambiguous the moment a query joins two of them.
_SHARED_CATALOG_COLUMNS = ("relname", "schemaname", "relid", "indexrelid", "oid")


def _sql_literals(text):
    """Every SQL-looking string literal in the module, joined per statement.

    The collector builds SQL by adjacent-string concatenation across lines, so
    reconstruct each statement by taking a SELECT and everything up to the
    closing paren of the _query call."""
    statements = []
    for match in re.finditer(r'"(SELECT .*?)"\s*\)', text, re.S):
        raw = match.group(1)
        # collapse the adjacent-literal quoting: `"a " \n "b "` -> `a b `
        joined = re.sub(r'"\s*\n\s*"', " ", raw)
        statements.append(" ".join(joined.split()))
    return statements


def test_joined_catalog_queries_qualify_their_shared_columns():
    text = _SRC.read_text(encoding="utf-8")
    offenders = []
    for sql in _sql_literals(text):
        if " JOIN " not in sql.upper():
            continue
        # only the SELECT list matters for ambiguity errors we can detect here
        select_list = sql[len("SELECT "):sql.upper().index(" FROM ")]
        for col in _SHARED_CATALOG_COLUMNS:
            # a bare column is one not preceded by `<alias>.`
            if re.search(r"(?<![\w.])" + col + r"\b", select_list):
                offenders.append((col, sql[:120]))
    assert not offenders, (
        "unqualified shared catalog column(s) in a JOINed query: "
        + "; ".join(f"{c} in [{s}]" for c, s in offenders)
        + "\nPostgreSQL rejects these with 'column reference ... is ambiguous', "
        "and the per-check try/except turns that into a silently missing check."
    )


def test_txid_check_still_selects_what_the_parser_expects():
    """Guard the fix itself: the txid query must qualify schemaname and relname
    and still return them in the order the record unpacking assumes
    (schema, relname, table_age)."""
    text = _SRC.read_text(encoding="utf-8")
    txid = next(s for s in _sql_literals(text) if "relfrozenxid" in s)
    assert "s.schemaname" in txid and "s.relname" in txid
    assert txid.index("s.schemaname") < txid.index("s.relname") < txid.index("age(c.relfrozenxid)")
    # the WHERE clause filters on the stats relation, not pg_class
    assert "WHERE s.schemaname NOT IN" in txid


def test_the_module_still_imports_and_declares_the_txid_thresholds():
    """Cheap smoke so a syntax slip in the SQL block cannot pass unnoticed."""
    spec = importlib.util.spec_from_file_location("pg_health_checks", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.TXID_CRITICAL > mod.TXID_WARN > 0
