ACTIVITY_SQL = """
SELECT
  COALESCE(state, 'unknown') AS state,
  COUNT(*) AS cnt
FROM pg_stat_activity
WHERE pid <> pg_backend_pid()
GROUP BY state
"""


LONG_RUNNING_SQL = """
SELECT
  pid,
  usename,
  state,
  EXTRACT(EPOCH FROM (NOW() - query_start)) AS duration_sec,
  EXTRACT(EPOCH FROM (NOW() - xact_start)) AS xact_duration_sec,
  LEFT(query, 200) AS query_text,
  wait_event_type,
  wait_event,
  client_addr::text AS client_addr
FROM pg_stat_activity
WHERE state != 'idle'
  AND pid <> pg_backend_pid()
  AND query_start IS NOT NULL
  AND EXTRACT(EPOCH FROM (NOW() - query_start)) > 5
ORDER BY query_start ASC
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


def _double(field):
    return field.get("doubleValue", 0.0) if not field.get("isNull") else 0.0


def collect_pg_activity(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, database):
    inserted = {"activity_states": 0, "long_running": 0}

    state_resp = rds_data_client.execute_statement(
        resourceArn=target_cluster_arn,
        secretArn=target_secret_arn,
        database=database,
        sql=f"/* source=dbops-etl */ {ACTIVITY_SQL}",
        includeResultMetadata=True,
    )

    state_map = {
        "active": "conn_active",
        "idle": "conn_idle",
        "idle in transaction": "conn_idle_in_tx",
        "idle in transaction (aborted)": "conn_idle_in_tx_aborted",
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
            "duration_sec": _double(rec[3]),
            "xact_duration_sec": _double(rec[4]),
            "query_text": _str(rec[5]),
            "wait_event_type": _str(rec[6]),
            "wait_event": _str(rec[7]),
            "client_addr": _str(rec[8]),
        })
        inserted["long_running"] += 1

    return {"cluster_id": cluster_id, **inserted}
