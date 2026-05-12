"""MySQL counterpart to pg_activity.py.

PG pg_stat_activity → MySQL performance_schema.threads (or processlist).

State mapping:
  - PG `active`               → MySQL PROCESSLIST_COMMAND='Query'
  - PG `idle`                 → PROCESSLIST_COMMAND='Sleep'
  - PG `idle in transaction`  → PROCESSLIST_COMMAND='Sleep' with an active TRX
                                (skipped — `information_schema.innodb_trx`
                                join is fragile; reported as `conn_other`)

Long-running queries come from threads with PROCESSLIST_COMMAND='Query' and
PROCESSLIST_TIME > 5 (seconds).
"""

ACTIVITY_SQL = """
SELECT
  COALESCE(PROCESSLIST_COMMAND, 'unknown') AS state,
  COUNT(*) AS cnt
FROM performance_schema.threads
WHERE TYPE = 'FOREGROUND'
  AND PROCESSLIST_ID IS NOT NULL
GROUP BY PROCESSLIST_COMMAND
"""


LONG_RUNNING_SQL = """
SELECT
  PROCESSLIST_ID                                AS pid,
  COALESCE(PROCESSLIST_USER, '')                AS username,
  COALESCE(PROCESSLIST_COMMAND, '')             AS state,
  COALESCE(PROCESSLIST_TIME, 0)                 AS duration_sec,
  0                                             AS xact_duration_sec,
  COALESCE(LEFT(PROCESSLIST_INFO, 200), '')     AS query_text,
  COALESCE(PROCESSLIST_STATE, '')               AS wait_event_type,
  ''                                            AS wait_event,
  COALESCE(PROCESSLIST_HOST, '')                AS client_addr
FROM performance_schema.threads
WHERE TYPE = 'FOREGROUND'
  AND PROCESSLIST_COMMAND = 'Query'
  AND PROCESSLIST_TIME > 5
  AND PROCESSLIST_INFO IS NOT NULL
ORDER BY PROCESSLIST_TIME DESC
LIMIT 20
"""


INSERT_METRIC = (
    "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
    "VALUES (:cluster_id, NOW(), :metric_type, :value, '{}'::jsonb) "
    "ON CONFLICT DO NOTHING"
)

INSERT_LONG = (
    "INSERT INTO long_running_queries "
    "(cluster_id, snapshot_time, pid, username, state, duration_sec, "
    " xact_duration_sec, query_text, wait_event_type, wait_event, client_addr) "
    "VALUES (:cluster_id, NOW(), :pid, :username, :state, :duration_sec, "
    " :xact_duration_sec, :query_text, :wait_event_type, :wait_event, :client_addr)"
)


def _str(field):
    return field.get("stringValue", "") if not field.get("isNull") else ""


def _long(field):
    return field.get("longValue", 0) if not field.get("isNull") else 0


def collect_mysql_activity(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, database):
    inserted = {"activity_states": 0, "long_running": 0}

    # State breakdown
    state_resp = rds_data_client.execute_statement(
        resourceArn=target_cluster_arn,
        secretArn=target_secret_arn,
        database=database,
        sql=f"/* source=dbops-etl */ {ACTIVITY_SQL}",
        includeResultMetadata=True,
    )

    # Map MySQL command → existing PG-style metric name so dashboard panels
    # ("active connections" etc.) keep working without changes.
    state_map = {
        "Query": "conn_active",
        "Sleep": "conn_idle",
        "Connect": "conn_other",
        "Daemon": "conn_other",
    }

    for rec in state_resp.get("records", []):
        state = _str(rec[0])
        count = _long(rec[1])
        metric = state_map.get(state, "conn_other")
        cache_execute(INSERT_METRIC, {
            "cluster_id": cluster_id,
            "metric_type": metric,
            "value": float(count),
        })
        inserted["activity_states"] += 1

    # Long-running queries
    long_resp = rds_data_client.execute_statement(
        resourceArn=target_cluster_arn,
        secretArn=target_secret_arn,
        database=database,
        sql=f"/* source=dbops-etl */ {LONG_RUNNING_SQL}",
        includeResultMetadata=True,
    )
    for rec in long_resp.get("records", []):
        cache_execute(INSERT_LONG, {
            "cluster_id": cluster_id,
            "pid": _long(rec[0]),
            "username": _str(rec[1]),
            "state": _str(rec[2]),
            "duration_sec": float(_long(rec[3])),
            "xact_duration_sec": 0.0,
            "query_text": _str(rec[5]),
            "wait_event_type": _str(rec[6]),
            "wait_event": _str(rec[7]),
            "client_addr": _str(rec[8]),
        })
        inserted["long_running"] += 1

    return {"cluster_id": cluster_id, **inserted}
