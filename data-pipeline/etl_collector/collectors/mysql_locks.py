"""MySQL counterpart to pg_locks.py (blocking locks + settings).

Uses sys.innodb_lock_waits for the standard "who is blocked by whom" view,
joined to performance_schema.threads for usernames. Settings are pulled from
global variables that matter to capacity / autovacuum-equivalent behavior.
"""

BLOCKING_LOCKS_SQL = """
SELECT
  lw.waiting_pid                          AS blocked_pid,
  COALESCE(wt.PROCESSLIST_USER, '')       AS blocked_user,
  lw.blocking_pid                         AS blocking_pid,
  COALESCE(bt.PROCESSLIST_USER, '')       AS blocking_user,
  COALESCE(LEFT(lw.waiting_query, 200), '')   AS blocked_query,
  COALESCE(LEFT(lw.blocking_query, 200), '')  AS blocking_query,
  COALESCE(lw.locked_type, '')            AS locktype,
  COALESCE(lw.waiting_lock_mode, '')      AS blocked_mode,
  COALESCE(lw.blocking_lock_mode, '')     AS blocking_mode,
  CONCAT(IFNULL(lw.locked_table_schema, '?'), '.', IFNULL(lw.locked_table_name, '?')) AS relation,
  lw.wait_age_secs                        AS blocked_duration_sec
FROM sys.innodb_lock_waits lw
LEFT JOIN performance_schema.threads wt ON wt.PROCESSLIST_ID = lw.waiting_pid
LEFT JOIN performance_schema.threads bt ON bt.PROCESSLIST_ID = lw.blocking_pid
LIMIT 50
"""


SETTINGS_SQL = """
SELECT VARIABLE_NAME AS name, VARIABLE_VALUE AS value, '' AS unit
FROM performance_schema.global_variables
WHERE VARIABLE_NAME IN (
  'max_connections', 'innodb_buffer_pool_size', 'innodb_log_file_size',
  'innodb_flush_log_at_trx_commit', 'innodb_io_capacity',
  'innodb_read_io_threads', 'innodb_write_io_threads',
  'slow_query_log', 'long_query_time', 'log_bin',
  -- Per-connection 버퍼 — mysql_param_fitness의 OOM 상호작용 규칙
  -- (Σ버퍼 × max_connections vs 인스턴스 메모리)에 쓰인다. 모두 바이트.
  'sort_buffer_size', 'join_buffer_size', 'read_buffer_size',
  'read_rnd_buffer_size', 'tmp_table_size', 'max_heap_table_size',
  'thread_stack'
)
ORDER BY VARIABLE_NAME
"""


INSERT_BLOCK_SQL = (
    "INSERT INTO blocking_locks "
    "(cluster_id, snapshot_time, blocked_pid, blocked_user, blocking_pid, blocking_user, "
    " blocked_query, blocking_query, locktype, blocked_mode, blocking_mode, relation, "
    " blocked_duration_sec) "
    "VALUES (:cluster_id, NOW(), :blocked_pid, :blocked_user, :blocking_pid, :blocking_user, "
    " :blocked_query, :blocking_query, :locktype, :blocked_mode, :blocking_mode, :relation, "
    " :blocked_duration_sec)"
)


UPSERT_SETTING_SQL = (
    "INSERT INTO cluster_settings (cluster_id, name, value, unit, updated_at) "
    "VALUES (:cluster_id, :name, :value, :unit, NOW()) "
    "ON CONFLICT (cluster_id, name) DO UPDATE SET "
    "  value = EXCLUDED.value, unit = EXCLUDED.unit, updated_at = NOW()"
)


def _str(field):
    return field.get("stringValue", "") if not field.get("isNull") else ""


def _long(field):
    return field.get("longValue", 0) if not field.get("isNull") else 0


def _double(field):
    if field.get("isNull"):
        return 0.0
    return field.get("doubleValue") or float(field.get("longValue") or 0)


def collect_mysql_locks(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, database):
    inserted = {"blocking_locks": 0, "settings": 0}

    # Blocking locks (sys.innodb_lock_waits may be empty when nothing is blocked).
    try:
        resp = rds_data_client.execute_statement(
            resourceArn=target_cluster_arn,
            secretArn=target_secret_arn,
            database=database,
            sql=f"/* source=dbops-etl */ {BLOCKING_LOCKS_SQL}",
            includeResultMetadata=True,
        )
        for rec in resp.get("records", []):
            cache_execute(INSERT_BLOCK_SQL, {
                "cluster_id": cluster_id,
                "blocked_pid": _long(rec[0]),
                "blocked_user": _str(rec[1]),
                "blocking_pid": _long(rec[2]),
                "blocking_user": _str(rec[3]),
                "blocked_query": _str(rec[4]),
                "blocking_query": _str(rec[5]),
                "locktype": _str(rec[6]),
                "blocked_mode": _str(rec[7]),
                "blocking_mode": _str(rec[8]),
                "relation": _str(rec[9]),
                "blocked_duration_sec": _double(rec[10]),
            })
            inserted["blocking_locks"] += 1
    except Exception as e:
        # sys.innodb_lock_waits may not exist on very old Aurora MySQL — log and continue.
        print(f"[mysql_locks] blocking-locks query failed: {e}")

    # Settings
    try:
        resp = rds_data_client.execute_statement(
            resourceArn=target_cluster_arn,
            secretArn=target_secret_arn,
            database=database,
            sql=f"/* source=dbops-etl */ {SETTINGS_SQL}",
            includeResultMetadata=True,
        )
        for rec in resp.get("records", []):
            cache_execute(UPSERT_SETTING_SQL, {
                "cluster_id": cluster_id,
                "name": _str(rec[0]),
                "value": _str(rec[1]),
                "unit": _str(rec[2]),
            })
            inserted["settings"] += 1
    except Exception as e:
        print(f"[mysql_locks] settings query failed: {e}")

    return {"cluster_id": cluster_id, **inserted}
