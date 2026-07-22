"""SQL Server wait statistics → metric_snapshots (feeds the Task-5 internals panel).

sys.dm_os_wait_stats is the engine-internal signal CloudWatch does NOT expose:
cumulative wait time per wait type since the last server restart / DBCC
SQLPERF reset. We emit the top waits as an engine-scoped `mssql_wait_ms` gauge
dimensioned by wait_type, so it never collides with the MySQL innodb_* metrics.

The benign idle/background waits are excluded (a well-known list — server
housekeeping that is always "waiting" and would otherwise dominate the top-N).
"""
import json

# Benign idle/background waits — always present, not a performance signal.
# Trimmed subset of the standard sys.dm_os_wait_stats exclusion list.
WAITS_SQL = """
SELECT TOP 20
  wait_type,
  wait_time_ms,
  waiting_tasks_count
FROM sys.dm_os_wait_stats
WHERE waiting_tasks_count > 0
  AND wait_type NOT IN (
    'CLR_SEMAPHORE', 'LAZYWRITER_SLEEP', 'RESOURCE_QUEUE', 'SLEEP_TASK',
    'SLEEP_SYSTEMTASK', 'SQLTRACE_BUFFER_FLUSH', 'WAITFOR', 'LOGMGR_QUEUE',
    'CHECKPOINT_QUEUE', 'REQUEST_FOR_DEADLOCK_SEARCH', 'XE_TIMER_EVENT',
    'BROKER_TO_FLUSH', 'BROKER_TASK_STOP', 'CLR_MANUAL_EVENT', 'CLR_AUTO_EVENT',
    'DISPATCHER_QUEUE_SEMAPHORE', 'FT_IFTS_SCHEDULER_IDLE_WAIT',
    'XE_DISPATCHER_WAIT', 'XE_DISPATCHER_JOIN', 'BROKER_EVENTHANDLER',
    'TRACEWRITE', 'BROKER_RECEIVE_WAITFOR', 'ONDEMAND_TASK_QUEUE',
    'DBMIRROR_EVENTS_QUEUE', 'DBMIRRORING_CMD', 'BROKER_TRANSMITTER',
    'SLEEP_BPOOL_FLUSH', 'SP_SERVER_DIAGNOSTICS_SLEEP', 'DIRTY_PAGE_POLL',
    'HADR_FILESTREAM_IOMGR_IOCOMPLETION', 'HADR_WORK_QUEUE', 'QDS_ASYNC_QUEUE'
  )
ORDER BY wait_time_ms DESC
"""


INSERT_METRIC = (
    "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
    "VALUES (:cluster_id, NOW(), :metric_type, :value, :dimensions::jsonb) "
    "ON CONFLICT DO NOTHING"
)


def _str(field):
    return field.get("stringValue", "") if not field.get("isNull") else ""


def _long(field):
    return field.get("longValue", 0) if not field.get("isNull") else 0


def collect_mssql_waits(rds_data_client, cache_execute, target_cluster_arn, target_secret_arn, cluster_id, database):
    resp = rds_data_client.execute_statement(
        resourceArn=target_cluster_arn,
        secretArn=target_secret_arn,
        database=database,
        sql=f"/* source=dbops-etl */ {WAITS_SQL}",
    )

    inserted = 0
    for rec in resp.get("records", []):
        wait_type = _str(rec[0])
        cache_execute(INSERT_METRIC, {
            "cluster_id": cluster_id,
            "metric_type": "mssql_wait_ms",
            "value": float(_long(rec[1])),
            "dimensions": json.dumps({"wait_type": wait_type}),
        })
        inserted += 1

    return {"cluster_id": cluster_id, "waits_collected": inserted}
