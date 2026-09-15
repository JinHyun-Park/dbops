"""SQL Server counterpart to mysql_query_stats.py.

RDS SQL Server exposes cumulative statement aggregates via
sys.dm_exec_query_stats (one row per compiled plan, since the plan entered
cache). That is FINER-grained than MySQL's events_statements_summary_by_digest
/ PG's pg_stat_statements, which emit exactly ONE row per query_hash: a single
query_hash can have multiple cached plans, so a raw DMV snapshot holds several
rows sharing one hash. query_regression PARTITIONs BY query_hash and LAGs by
snapshot_time, so duplicate same-tick hashes corrupt the per-interval delta —
we GROUP BY query_hash here to emit exactly one row per hash per snapshot,
matching the MySQL/PG shape. Output goes to the SAME `query_stats` cache table.

UNIT: dm_exec_query_stats time columns are MICROSECONDS (µs) → /1000.0 = ms.
(NOT picoseconds like MySQL perf_schema TIMER_WAIT, and NOT milliseconds like
dm_exec_requests, see mssql_activity.) The /1000.0 is done in SQL.

LITERALS: this is the ONLY one of the three query_stats producers whose source
column is a raw statement. pg_stat_statements.query already arrives with
constants replaced by $N, performance_schema.DIGEST_TEXT with ?, but
sys.dm_exec_sql_text().text is the statement as sent, literals intact, so user
data would land in query_stats.query_text on SQL Server only. Five UI
components render that column verbatim to authenticated users, so the scrub
belongs here at the single producer, not in one reader. scrub_sql_literals
below normalizes to the same shape the other two engines already deliver.
"""

import re

QUERY_STATS_SQL = """
SELECT TOP 100
  CONVERT(VARCHAR(64), qs.query_hash, 2)                              AS query_hash,
  MAX(SUBSTRING(st.text, (qs.statement_start_offset/2)+1, 500))       AS query_text,
  SUM(qs.execution_count)                                             AS calls,
  SUM(qs.total_elapsed_time)/1000.0                                   AS total_time_ms,
  (SUM(qs.total_elapsed_time)/NULLIF(SUM(qs.execution_count),0))/1000.0 AS mean_time_ms,
  SUM(qs.total_rows)                                                  AS rows_returned
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
WHERE st.text IS NOT NULL
GROUP BY qs.query_hash
ORDER BY SUM(qs.total_elapsed_time) DESC
"""


INSERT_SQL = (
    "INSERT INTO query_stats "
    "(cluster_id, snapshot_time, query_hash, query_text, calls, "
    " total_time_ms, mean_time_ms, rows_returned) "
    "VALUES (:cluster_id, NOW(), :query_hash, :query_text, :calls, "
    " :total_time_ms, :mean_time_ms, :rows_returned)"
)


# Numeric literal forms: hex blob, integer/decimal with optional exponent, and
# a leading-dot decimal. Matched only where the preceding character cannot be
# part of an identifier, so `table1` and `a1b2` keep their digits.
_NUMERIC = re.compile(
    r"0[xX][0-9A-Fa-f]+"
    r"|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?"
    r"|\.\d+(?:[eE][+-]?\d+)?"
)


def _in_identifier(text, i):
    """True if position i continues an identifier that started earlier."""
    if i == 0:
        return False
    prev = text[i - 1]
    return prev.isalnum() or prev in "_@#$"


def scrub_sql_literals(text):
    """Replace constants in a raw T-SQL statement so no user data is stored.

    String literals become '?' and numeric literals become ?, matching what
    pg_stat_statements and performance_schema.DIGEST_TEXT already hand the
    other two collectors. Keywords, identifiers (bare, [bracketed] and
    "quoted"), operators, comments and parameter markers such as @p1 are left
    exactly as they were.

    One left-to-right pass, NOT a sequence of regex replaces, because the
    interleaving is the whole point: a quote inside a comment is a comment, an
    apostrophe in `-- don't` must not open a literal that swallows the real
    literal after it, and a digit inside a string must not be scrubbed twice.
    An unterminated literal (the DMV SUBSTRING truncates at 500 chars, often
    mid-literal) is consumed to end of text, so a cut-off constant is still
    replaced rather than left bare.

    ponytail: constants only. Data interpolated into an IDENTIFIER position by
    dynamic SQL stays visible; catching that needs a real T-SQL parser, and no
    known producer here does it.
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "'":                                   # '...' string literal
            i += 1
            while i < n:
                if text[i] == "'":
                    if text[i + 1:i + 2] == "'":       # '' escaped quote, data
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("'?'")
        elif c == "-" and nxt == "-":                  # line comment
            end = text.find("\n", i)
            end = n if end == -1 else end + 1
            out.append(text[i:end])
            i = end
        elif c == "/" and nxt == "*":                  # block comment
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(text[i:end])
            i = end
        elif c == "[" or c == '"':                     # quoted identifier
            close = "]" if c == "[" else '"'
            j = i + 1
            while j < n:
                if text[j] == close:
                    if text[j + 1:j + 2] == close:     # ]] / "" escaped close
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
        elif (c.isdigit() or (c == "." and nxt.isdigit())) and not _in_identifier(text, i):
            m = _NUMERIC.match(text, i)
            out.append("?")
            i = m.end()
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _str(field):
    return field.get("stringValue", "") if not field.get("isNull") else ""


def _long(field):
    return field.get("longValue", 0) if not field.get("isNull") else 0


def _double(field):
    if field.get("isNull"):
        return 0.0
    return field.get("doubleValue") or float(field.get("longValue") or 0)


def collect_mssql_query_stats(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, database):
    resp = rds_data_client.execute_statement(
        resourceArn=target_cluster_arn,
        secretArn=target_secret_arn,
        database=database,
        sql=f"/* source=dbops-etl */ {QUERY_STATS_SQL}",
    )

    inserted = 0
    for rec in resp.get("records", []):
        params = {
            "cluster_id": cluster_id,
            "query_hash": _str(rec[0]),
            "query_text": scrub_sql_literals(_str(rec[1]))[:4000],
            "calls": _long(rec[2]),
            "total_time_ms": _double(rec[3]),
            "mean_time_ms": _double(rec[4]),
            "rows_returned": _long(rec[5]),
        }
        cache_execute(INSERT_SQL, params)
        inserted += 1

    return {"cluster_id": cluster_id, "queries_collected": inserted}
