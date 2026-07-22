"""SQL Server counterpart to mysql_activity.py + the blocking half of mysql_locks.py.

Three DMV reads → the SAME cache tables the MySQL collectors write:
  - session state breakdown (sys.dm_exec_sessions) → metric_snapshots
    conn_active / conn_idle / conn_other. A user session's status is 'running'
    while it has an active request and 'sleeping' when idle — the exact
    PROCESSLIST 'Query'/'Sleep' distinction the MySQL collector maps.
  - long-running requests (sys.dm_exec_requests) → long_running_queries.
  - blocked requests (blocking_session_id <> 0) → blocking_locks.

UNIT: dm_exec_requests.total_elapsed_time / wait_time are MILLISECONDS (ms)
→ /1000.0 = seconds. (dm_exec_query_stats is µs — different DMV, see
mssql_query_stats.) The >5000 long-running threshold is 5000 ms = 5 s, matching
the MySQL collector's PROCESSLIST_TIME > 5 (seconds).
"""

ACTIVITY_SQL = """
SELECT LOWER(s.status) AS state, COUNT(*) AS cnt
FROM sys.dm_exec_sessions s
WHERE s.is_user_process = 1
GROUP BY s.status
"""


LONG_RUNNING_SQL = """
SELECT TOP 20
  r.session_id                              AS pid,
  COALESCE(s.login_name, '')                AS username,
  COALESCE(r.status, '')                    AS state,
  r.total_elapsed_time/1000.0               AS duration_sec,
  COALESCE(SUBSTRING(st.text, 1, 200), '')  AS query_text,
  COALESCE(r.wait_type, '')                 AS wait_event_type,
  COALESCE(s.host_name, '')                 AS client_addr
FROM sys.dm_exec_requests r
JOIN sys.dm_exec_sessions s ON r.session_id = s.session_id
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) st
WHERE s.is_user_process = 1
  AND r.total_elapsed_time > 5000
ORDER BY r.total_elapsed_time DESC
"""


BLOCKING_SQL = """
SELECT TOP 50
  r.session_id                              AS blocked_pid,
  COALESCE(s.login_name, '')                AS blocked_user,
  r.blocking_session_id                     AS blocking_pid,
  COALESCE(bs.login_name, '')               AS blocking_user,
  COALESCE(SUBSTRING(bt.text, 1, 200), '')  AS blocked_query,
  COALESCE(SUBSTRING(kt.text, 1, 200), '')  AS blocking_query,
  COALESCE(r.wait_type, '')                 AS locktype,
  COALESCE(r.wait_resource, '')             AS relation,
  r.wait_time/1000.0                        AS blocked_duration_sec
FROM sys.dm_exec_requests r
JOIN sys.dm_exec_sessions s ON r.session_id = s.session_id
LEFT JOIN sys.dm_exec_sessions bs ON bs.session_id = r.blocking_session_id
LEFT JOIN sys.dm_exec_requests br ON br.session_id = r.blocking_session_id
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) bt
OUTER APPLY sys.dm_exec_sql_text(br.sql_handle) kt
WHERE r.blocking_session_id <> 0
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

INSERT_BLOCK = (
    "INSERT INTO blocking_locks "
    "(cluster_id, snapshot_time, blocked_pid, blocked_user, blocking_pid, blocking_user, "
    " blocked_query, blocking_query, locktype, blocked_mode, blocking_mode, relation, "
    " blocked_duration_sec) "
    "VALUES (:cluster_id, NOW(), :blocked_pid, :blocked_user, :blocking_pid, :blocking_user, "
    " :blocked_query, :blocking_query, :locktype, :blocked_mode, :blocking_mode, :relation, "
    " :blocked_duration_sec)"
)


def _str(field):
    return field.get("stringValue", "") if not field.get("isNull") else ""


def _long(field):
    return field.get("longValue", 0) if not field.get("isNull") else 0


def _double(field):
    if field.get("isNull"):
        return 0.0
    return field.get("doubleValue") or float(field.get("longValue") or 0)


def _read(client, sql, cluster_id, database):
    return client.execute_statement(
        resourceArn="", secretArn="", database=database,
        sql=f"/* source=dbops-etl */ {sql}",
    )


def collect_mssql_activity(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, database):
    inserted = {"activity_states": 0, "long_running": 0, "blocking_locks": 0}

    # State breakdown → PG-style metric names so the dashboard "active
    # connections" panels keep working unchanged.
    state_map = {"running": "conn_active", "sleeping": "conn_idle"}
    for rec in _read(rds_data_client, ACTIVITY_SQL, cluster_id, database).get("records", []):
        metric = state_map.get(_str(rec[0]).lower(), "conn_other")
        cache_execute(INSERT_METRIC, {
            "cluster_id": cluster_id,
            "metric_type": metric,
            "value": float(_long(rec[1])),
        })
        inserted["activity_states"] += 1

    for rec in _read(rds_data_client, LONG_RUNNING_SQL, cluster_id, database).get("records", []):
        cache_execute(INSERT_LONG, {
            "cluster_id": cluster_id,
            "pid": _long(rec[0]),
            "username": _str(rec[1]),
            "state": _str(rec[2]),
            "duration_sec": _double(rec[3]),
            "xact_duration_sec": 0.0,
            "query_text": _str(rec[4]),
            "wait_event_type": _str(rec[5]),
            "wait_event": "",
            "client_addr": _str(rec[6]),
        })
        inserted["long_running"] += 1

    for rec in _read(rds_data_client, BLOCKING_SQL, cluster_id, database).get("records", []):
        cache_execute(INSERT_BLOCK, {
            "cluster_id": cluster_id,
            "blocked_pid": _long(rec[0]),
            "blocked_user": _str(rec[1]),
            "blocking_pid": _long(rec[2]),
            "blocking_user": _str(rec[3]),
            "blocked_query": _str(rec[4]),
            "blocking_query": _str(rec[5]),
            "locktype": _str(rec[6]),
            "blocked_mode": "",
            "blocking_mode": "",
            "relation": _str(rec[7]),
            "blocked_duration_sec": _double(rec[8]),
        })
        inserted["blocking_locks"] += 1

    return {"cluster_id": cluster_id, **inserted}
