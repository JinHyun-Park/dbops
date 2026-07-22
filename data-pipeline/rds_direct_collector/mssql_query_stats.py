"""SQL Server counterpart to mysql_query_stats.py.

RDS SQL Server exposes cumulative statement aggregates via
sys.dm_exec_query_stats (per compiled plan, since the plan entered cache) —
same model as MySQL's events_statements_summary_by_digest / PG's
pg_stat_statements, so the existing dashboard math works. Output goes to the
SAME `query_stats` cache table the MySQL collector writes.

UNIT: dm_exec_query_stats time columns are MICROSECONDS (µs) → /1000.0 = ms.
(NOT picoseconds like MySQL perf_schema TIMER_WAIT, and NOT milliseconds like
dm_exec_requests — see mssql_activity.) The /1000.0 is done in SQL.
"""

QUERY_STATS_SQL = """
SELECT TOP 100
  CONVERT(VARCHAR(64), qs.query_hash, 2)                       AS query_hash,
  SUBSTRING(st.text, (qs.statement_start_offset/2)+1, 500)     AS query_text,
  qs.execution_count                                           AS calls,
  qs.total_elapsed_time/1000.0                                 AS total_time_ms,
  (qs.total_elapsed_time/NULLIF(qs.execution_count,0))/1000.0  AS mean_time_ms,
  qs.total_rows                                                AS rows_returned
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
WHERE st.text IS NOT NULL
ORDER BY qs.total_elapsed_time DESC
"""


INSERT_SQL = (
    "INSERT INTO query_stats "
    "(cluster_id, snapshot_time, query_hash, query_text, calls, "
    " total_time_ms, mean_time_ms, rows_returned) "
    "VALUES (:cluster_id, NOW(), :query_hash, :query_text, :calls, "
    " :total_time_ms, :mean_time_ms, :rows_returned)"
)


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
            "query_text": _str(rec[1])[:4000],
            "calls": _long(rec[2]),
            "total_time_ms": _double(rec[3]),
            "mean_time_ms": _double(rec[4]),
            "rows_returned": _long(rec[5]),
        }
        cache_execute(INSERT_SQL, params)
        inserted += 1

    return {"cluster_id": cluster_id, "queries_collected": inserted}
