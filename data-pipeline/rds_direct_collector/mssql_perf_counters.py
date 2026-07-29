"""SQL Server internal gauges from sys.dm_os_performance_counters (E-3).

Why this exists: the rds_instance internals surface for SQL Server had exactly
one signal, sys.dm_os_wait_stats (mssql_waits.py). The MySQL half of the same
family already publishes 5 InnoDB gauges. This adds the SQL Server equivalents.

THE TRAP THIS COLLECTOR EXISTS TO AVOID (all values below MEASURED live on
dbops-demo-mssql through the deployed operations MCP Lambda):

  sys.dm_os_performance_counters mixes THREE cntr_type semantics in ONE result
  set, and `cntr_value` alone is meaningless without the type:

    537003264  ratio NUMERATOR. Needs its paired `... base` row (cntr_type
               1073939712) as the denominator. MEASURED:
               `Buffer cache hit ratio` = 1980 with base = 1980, i.e. 100%.
               Publishing 1980 (or "1980%") for a perfectly healthy cache is
               exactly the confidently-wrong answer this program removes.
    65792      raw instantaneous value, safe to publish as-is. MEASURED:
               `Page life expectancy` = 9938 (seconds),
               `Memory Grants Pending` = 0, `Processes blocked` = 0.
    272696576  CUMULATIVE since server start. MEASURED `Batch Requests/sec` =
               1,825,989, which is a total, not a rate. Turning it into a
               rate needs a second sample.

  So this collector publishes ONLY DERIVED or genuinely instantaneous values and
  deliberately writes NO cntr_type 272696576 counter. See the note at the bottom
  for why the cumulative ones are left out rather than published raw.

Multi-row hazard, also MEASURED: several of these counter names exist under more
than one object with a non-empty instance_name (`Page life expectancy` also
appears under `SQLServer:Buffer Node` instance '000'; `Lock Waits/sec` has 15
instance rows including '_Total'). Every read here pins BOTH object_name and
instance_name = '' so one counter can never be double counted.
"""
import json

# (object_name, counter_name) -> we read cntr_value at instance_name = ''.
# Every pair below was MEASURED PRESENT on sqlserver-ex 15.00.4470.1.v1.
_BUFFER_MGR = "SQLServer:Buffer Manager"
_MEMORY_MGR = "SQLServer:Memory Manager"
_GENERAL = "SQLServer:General Statistics"

COUNTERS_SQL = """
SELECT
  RTRIM(object_name)  AS obj,
  RTRIM(counter_name) AS cnt,
  cntr_value,
  cntr_type
FROM sys.dm_os_performance_counters
WHERE RTRIM(instance_name) = ''
  AND (
    (RTRIM(object_name) = 'SQLServer:Buffer Manager'
     AND RTRIM(counter_name) IN ('Buffer cache hit ratio',
                                 'Buffer cache hit ratio base',
                                 'Page life expectancy'))
 OR (RTRIM(object_name) = 'SQLServer:Memory Manager'
     AND RTRIM(counter_name) IN ('Memory Grants Pending',
                                 'Total Server Memory (KB)',
                                 'Target Server Memory (KB)'))
 OR (RTRIM(object_name) = 'SQLServer:General Statistics'
     AND RTRIM(counter_name) = 'Processes blocked')
  )
""".strip()


INSERT_METRIC = (
    "INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions) "
    "VALUES (:cluster_id, NOW(), :metric_type, :value, :dimensions::jsonb) "
    "ON CONFLICT DO NOTHING"
)

# cntr_type constants, named so a reader does not have to look them up.
RATIO_NUMERATOR = 537003264
RATIO_BASE = 1073939712
RAW_VALUE = 65792


def _str(field):
    return field.get("stringValue", "") if not field.get("isNull") else ""


def _long(field):
    return field.get("longValue", 0) if not field.get("isNull") else 0


def collect_mssql_perf_counters(rds_data_client, cache_execute, target_cluster_arn,
                                target_secret_arn, cluster_id, database):
    resp = rds_data_client.execute_statement(
        resourceArn=target_cluster_arn,
        secretArn=target_secret_arn,
        database=database,
        sql=f"/* source=dbops-etl */ {COUNTERS_SQL}",
    )

    # (object, counter) -> (value, cntr_type). Nothing is derived until every row
    # is in hand, because the ratio needs two rows from the same snapshot.
    read = {}
    for rec in resp.get("records", []):
        read[(_str(rec[0]), _str(rec[1]))] = (_long(rec[2]), _long(rec[3]))

    metrics = {}
    skipped = {}

    def raw(obj, counter, metric_type, expect_type=RAW_VALUE):
        got = read.get((obj, counter))
        if got is None:
            skipped[metric_type] = "counter not present"
            return None
        value, cntr_type = got
        if cntr_type != expect_type:
            # The engine changed the semantics of this counter: refuse rather
            # than publish a number under the wrong meaning.
            skipped[metric_type] = f"unexpected cntr_type {cntr_type}"
            return None
        metrics[metric_type] = float(value)
        return float(value)

    # 1) Buffer cache hit ratio: numerator / base, NEVER the raw numerator.
    num = read.get((_BUFFER_MGR, "Buffer cache hit ratio"))
    base = read.get((_BUFFER_MGR, "Buffer cache hit ratio base"))
    if num is None or base is None:
        skipped["mssql_buffer_cache_hit_ratio"] = "numerator or base row missing"
    elif num[1] != RATIO_NUMERATOR or base[1] != RATIO_BASE:
        skipped["mssql_buffer_cache_hit_ratio"] = (
            f"unexpected cntr_type pair {num[1]}/{base[1]}")
    elif base[0] <= 0:
        # No page lookups since the counters last reset. A ratio is UNDEFINED
        # here, not 0% and not 100%: publishing either would be inventing a
        # measurement nothing supports.
        skipped["mssql_buffer_cache_hit_ratio"] = "base is 0 (no page lookups yet)"
    else:
        metrics["mssql_buffer_cache_hit_ratio"] = round(100.0 * num[0] / base[0], 2)

    # 2) Genuinely instantaneous gauges (cntr_type 65792), published as read.
    raw(_BUFFER_MGR, "Page life expectancy", "mssql_page_life_expectancy_sec")
    raw(_MEMORY_MGR, "Memory Grants Pending", "mssql_memory_grants_pending")
    raw(_GENERAL, "Processes blocked", "mssql_processes_blocked")

    # 3) Buffer-pool ramp: how much of the memory SQL Server WANTS it currently
    # holds. Both operands are cntr_type 65792 raw KB, so this is a same-snapshot
    # ratio like (1) and needs no history.
    total = read.get((_MEMORY_MGR, "Total Server Memory (KB)"))
    target = read.get((_MEMORY_MGR, "Target Server Memory (KB)"))
    if total is None or target is None:
        skipped["mssql_server_memory_used_pct"] = "total or target row missing"
    elif total[1] != RAW_VALUE or target[1] != RAW_VALUE:
        skipped["mssql_server_memory_used_pct"] = (
            f"unexpected cntr_type pair {total[1]}/{target[1]}")
    elif target[0] <= 0:
        skipped["mssql_server_memory_used_pct"] = "target server memory is 0"
    else:
        metrics["mssql_server_memory_used_pct"] = round(100.0 * total[0] / target[0], 2)

    # dimensions='{}' (cluster-level): the instance IS the monitored resource for
    # this family, the same convention rds_instance_cw_collector uses, so triage /
    # anomaly baselines / the batch-timeseries endpoint read these unmodified.
    for metric_type, value in metrics.items():
        cache_execute(INSERT_METRIC, {
            "cluster_id": cluster_id,
            "metric_type": metric_type,
            "value": value,
            "dimensions": json.dumps({}),
        })

    return {"cluster_id": cluster_id, "counters_read": len(read),
            "metrics_inserted": len(metrics), "metrics": metrics,
            "skipped": skipped}

# ponytail: the cntr_type 272696576 counters (Batch Requests/sec, Number of
# Deadlocks/sec, Lock Waits/sec, Transactions/sec) are deliberately NOT collected.
# They are cumulative, so a rate needs a previous sample, and metric_snapshots is
# the wrong place to park one: pg_baseline_trainer and proactive_monitor both
# iterate EVERY metric_type with no allowlist, so a monotonically rising series
# would train a baseline it always exceeds and emit a false anomaly every cycle,
# forever. Upgrade path when these are wanted: keep the previous cumulative
# OUTSIDE metric_snapshots (its own small table, or a second sample a few seconds
# apart inside one invocation) and publish only the derived rate.
