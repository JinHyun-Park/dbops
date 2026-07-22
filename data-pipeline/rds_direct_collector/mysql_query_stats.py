"""MySQL counterpart to stats_collector.py (PG pg_stat_statements).

Aurora MySQL 8.0+ exposes statement aggregates via
`performance_schema.events_statements_summary_by_digest`. Values are
cumulative since the digest was first seen — same model as PG's
pg_stat_statements.calls/total_time, so the existing dashboard math works.
"""

QUERY_STATS_SQL = """
SELECT
  COALESCE(DIGEST, 'UNKNOWN') AS query_hash,
  COALESCE(DIGEST_TEXT, '') AS query_text,
  COUNT_STAR AS calls,
  ROUND(SUM_TIMER_WAIT/1000000, 2) AS total_time_ms,
  ROUND(AVG_TIMER_WAIT/1000000, 2) AS mean_time_ms,
  SUM_ROWS_SENT AS rows_returned
FROM performance_schema.events_statements_summary_by_digest
WHERE SCHEMA_NAME IS NOT NULL
  AND DIGEST IS NOT NULL
  AND COUNT_STAR > 0
ORDER BY SUM_TIMER_WAIT DESC
LIMIT 100
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


def collect_mysql_query_stats(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, database):
    resp = rds_data_client.execute_statement(
        resourceArn=target_cluster_arn,
        secretArn=target_secret_arn,
        database=database,
        sql=f"/* source=dbops-etl */ {QUERY_STATS_SQL}",
        includeResultMetadata=True,
    )

    inserted = 0
    for rec in resp.get("records", []):
        digest = _str(rec[0])
        text = _str(rec[1])[:4000]
        params = {
            "cluster_id": cluster_id,
            "query_hash": digest,
            "query_text": text,
            "calls": _long(rec[2]),
            "total_time_ms": _double(rec[3]),
            "mean_time_ms": _double(rec[4]),
            "rows_returned": _long(rec[5]),
        }
        cache_execute(INSERT_SQL, params)
        inserted += 1

    return {"cluster_id": cluster_id, "queries_collected": inserted}
