BLOCKING_LOCKS_SQL = """
SELECT
  blocked.pid AS blocked_pid,
  blocked_act.usename AS blocked_user,
  blocking.pid AS blocking_pid,
  blocking_act.usename AS blocking_user,
  LEFT(blocked_act.query, 200) AS blocked_query,
  LEFT(blocking_act.query, 200) AS blocking_query,
  blocked.locktype,
  blocked.mode AS blocked_mode,
  blocking.mode AS blocking_mode,
  COALESCE(blocked.relation::regclass::text, '-') AS relation,
  EXTRACT(EPOCH FROM (NOW() - blocked_act.query_start)) AS blocked_duration_sec
FROM pg_catalog.pg_locks blocked
JOIN pg_catalog.pg_stat_activity blocked_act ON blocked_act.pid = blocked.pid
JOIN pg_catalog.pg_locks blocking
  ON blocking.locktype = blocked.locktype
  AND blocking.database IS NOT DISTINCT FROM blocked.database
  AND blocking.relation IS NOT DISTINCT FROM blocked.relation
  AND blocking.page IS NOT DISTINCT FROM blocked.page
  AND blocking.tuple IS NOT DISTINCT FROM blocked.tuple
  AND blocking.virtualxid IS NOT DISTINCT FROM blocked.virtualxid
  AND blocking.transactionid IS NOT DISTINCT FROM blocked.transactionid
  AND blocking.classid IS NOT DISTINCT FROM blocked.classid
  AND blocking.objid IS NOT DISTINCT FROM blocked.objid
  AND blocking.objsubid IS NOT DISTINCT FROM blocked.objsubid
  AND blocking.pid != blocked.pid
  AND blocking.granted
JOIN pg_catalog.pg_stat_activity blocking_act ON blocking_act.pid = blocking.pid
WHERE NOT blocked.granted
LIMIT 50
"""


SETTINGS_SQL = """
SELECT name, setting, unit
FROM pg_settings
WHERE name IN (
  -- Capacity / planner
  'max_connections', 'shared_buffers', 'work_mem', 'maintenance_work_mem',
  'effective_cache_size', 'wal_buffers', 'checkpoint_timeout',
  'autovacuum_max_workers', 'max_worker_processes',
  -- Logging (pgBadger / Maintenance Health recommendations)
  'log_checkpoints', 'log_connections', 'log_disconnections',
  'log_lock_waits', 'log_autovacuum_min_duration',
  'log_min_duration_statement', 'log_temp_files'
)
ORDER BY name
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
    return field.get("doubleValue", 0.0) if not field.get("isNull") else 0.0


def collect_pg_locks(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, database):
    inserted = {"blocking_locks": 0, "settings": 0}

    # Blocking locks
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

    # Settings (cluster_settings table)
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

    return {"cluster_id": cluster_id, **inserted}
