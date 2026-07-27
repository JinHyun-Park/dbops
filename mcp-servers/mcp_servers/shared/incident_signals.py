"""Per-family metric_type sets for the incident signal readers.

Why this exists: ``diagnose_root_cause`` and ``correlate_signals`` hardcoded
``metric_type IN ('aas','cpu','db_connections')``. Those three names are
Aurora's (PI ``db.load.avg`` -> aas, PI ``os.cpuUtilization.total.avg`` -> cpu,
CloudWatch ``DatabaseConnections`` -> db_connections). No other family writes
them under those names, so an RCA or a correlation timeline for DocumentDB,
DynamoDB, ElastiCache or a standalone RDS instance ranked ZERO metric signals.
The tool answered, it just had nothing in it, which a DBA reads as "no metric
evidence" rather than "this engine was never wired".

Every name below was copied from the collector that WRITES it:

  * relational   -> data-pipeline/etl_collector/collectors/pi_collector.py
                    (aas, cpu) + cw_collector.py (db_connections)
  * rds_instance -> rds_instance_cw_collector.py (_METRICS) + pi_collector.py
                    (PI_METRICS_RDS_INSTANCE: db.load.avg -> aas, the only PI
                    metric that works on both MySQL and SQL Server)
  * documentdb   -> docdb_cw_collector.py (_CLUSTER_METRICS/_INSTANCE_METRICS)
  * dynamodb     -> dynamodb_cw_collector.py (_TABLE_METRICS_SUM)
  * elasticache  -> elasticache_cw_collector.py (_REDIS_METRICS/_MEMCACHED_METRICS)

Do not add a name without opening its collector first. The audit draft that
prompted this work asked for a "consumed vs provisioned utilization" series that
NO collector produces (provisioned_rcu/wcu exist only for PROVISIONED tables and
are separate rows from consumed_rcu/wcu, never a ratio series).

Two names are deliberately absent even though the collectors write them:
  * ``latency_ms_get``/``latency_ms_query``/... (DynamoDB) carry
    ``dimensions = {"operation": ...}``, so the strict cluster-level filter these
    readers must use can never see them. Listing them would look like coverage
    and return nothing.
  * ``buffer_cache_hit`` (DocumentDB) and ``freeable_memory`` are DROP signals.
    Both readers only detect increases, so they would add query columns that can
    never fire.

GAUGES vs COUNTERS is the load-bearing distinction. The gauge path divides an
in-window average by the previous window's average, so it needs baseline > 0.
The most diagnostic non-relational signals are event counters that sit at
exactly 0 in healthy operation and only appear during an incident (DynamoDB
throttles, DocumentDB cursors_timed_out): a ratio against a zero baseline is
undefined, so the zero-baseline guard skipped precisely the metrics that matter.
Widening the allowlist alone would have changed nothing for them. Counters
therefore get their own path in diagnose_root_cause: leaving zero IS the signal,
and magnitude is scaled by the per-metric noise floor below instead of by the
baseline, so nothing ever divides by zero.

The floor is what keeps the counter path from degrading into an always-firing
alarm: a counter whose in-window total is below its floor contributes nothing.
Floor 1.0 for throttles / timed-out cursors matches the existing findings
collectors, which already treat ANY of these as reportable
(dynamodb_findings.py throttle rules, docdb_findings.py
``SUM(cursors_timed_out) > 0 -> warning``).
"""

from mcp_servers.shared.engine_family import (
    DOCUMENTDB,
    DYNAMODB,
    ELASTICACHE,
    RDS_INSTANCE,
    RELATIONAL,
)
from mcp_servers.shared.engine_family import engine_family as _engine_family

# family -> {
#   "gauges":         ratio-vs-baseline path (needs baseline > 0),
#   "counters":       metric_type -> in-window noise floor (zero-baseline path),
#   "timeline_extra": ranked elsewhere, but still belongs on a correlation
#                     timeline,
# }
SIGNAL_SETS = {
    # UNCHANGED from the hardcoded literal. Aurora behavior must stay identical:
    # same three series, no counter path (Aurora's only Sum metric is
    # `deadlocks`, which cw_collector writes at cluster level but which no
    # ranking has ever consumed; adding it here would change Aurora's answer).
    RELATIONAL: {
        "gauges": ("aas", "cpu", "db_connections"),
        "counters": {},
        "timeline_extra": (),
    },
    # Standalone RDS MySQL / SQL Server. Names differ from Aurora: no
    # replica_lag_ms and no buffer_cache_hit from CloudWatch, and `aas` only
    # exists when Performance Insights is on (t4g.micro does not support PI).
    RDS_INSTANCE: {
        "gauges": ("aas", "cpu", "db_connections", "read_latency", "write_latency", "swap_usage"),
        "counters": {},
        "timeline_extra": (),
    },
    # DocumentDB CPU is `cpu_utilization`, never `cpu`, the single name that
    # made DocumentDB RCA return zero metric signals.
    DOCUMENTDB: {
        "gauges": (
            "cpu_utilization",
            "db_connections",
            "replica_lag_ms",
            "read_latency_ms",
            "write_latency_ms",
        ),
        # Sum metric (DatabaseCursorsTimedOut), 0 in healthy operation.
        "counters": {"cursors_timed_out": 1.0},
        "timeline_extra": (),
    },
    # DynamoDB has no CPU/connection concept at all. consumed_rcu/wcu are Sum
    # metrics but are normally nonzero, so they belong on the ratio path;
    # throttles are the headline counters.
    # throttled_requests is deliberately NOT a third counter. One throttle storm
    # raises all three series at once, so ranking them independently produced
    # three near-identical candidates for a single event (measured: ranks 2/3/4,
    # all score 3.25, eating 3 of the 8 top slots). It also contradicts this
    # project's own aggregation rule: dynamodb_findings.py folds
    # throttled_requests into the WRITE side rather than counting it separately.
    # It stays on the correlation timeline, where seeing all three is useful.
    DYNAMODB: {
        "gauges": ("consumed_rcu", "consumed_wcu", "returned_item_count"),
        "counters": {
            "read_throttle_events": 1.0,
            "write_throttle_events": 1.0,
        },
        "timeline_extra": ("throttled_requests",),
    },
    # ElastiCache has two CPU series: cache_cpu (host) and engine_cpu (the
    # single-threaded engine, the one that actually saturates).
    # evictions/replication_lag are NOT counters here on purpose:
    # diagnose_root_cause._collect_elasticache_signals already ranks them with
    # per-datapoint evidence, so listing them again would double-rank the same
    # storm. They stay on the timeline via timeline_extra.
    ELASTICACHE: {
        "gauges": ("cache_cpu", "engine_cpu", "memory_usage_pct", "curr_connections", "swap_usage"),
        "counters": {},
        "timeline_extra": ("evictions", "replication_lag"),
    },
}


def signals_for(family):
    """Signal set for a family; unknown family -> the relational set."""
    return SIGNAL_SETS.get(family) or SIGNAL_SETS[RELATIONAL]


def timeline_metrics(family):
    """Every metric_type worth putting on a correlation timeline for a family."""
    sets = signals_for(family)
    return tuple(sets["gauges"]) + tuple(sets["counters"]) + tuple(sets["timeline_extra"])


def resolve_family(cache, cluster_id):
    """``(family, resolved)`` for a cluster, from ``cluster_meta.engine``.

    ``resolved=False`` means the lookup failed or the cluster is not in
    cluster_meta. Callers then keep the relational set (today's behavior) but
    MUST surface that they did: a silently relational metric set on a
    DocumentDB cluster is exactly the zero-signal bug this module fixes, and
    hiding it would be worse than the original hardcoded literal.
    """
    try:
        rows = cache.execute(
            "SELECT engine FROM cluster_meta WHERE cluster_id = :cluster_id",
            {"cluster_id": cluster_id},
        ).rows
    except Exception as e:
        print(f"[incident_signals] engine lookup failed for {cluster_id}: {e}")
        return RELATIONAL, False
    if not rows:
        return RELATIONAL, False
    return _engine_family(rows[0].get("engine")), True


def metric_in_clause(names, prefix="m"):
    """``("IN (:m0, :m1)", {"m0": ..., "m1": ...})``.

    The RDS Data API cannot bind a list, and interpolating the names would be a
    (constant-only, but still) SQL-building habit worth not having. Same
    placeholder shape as api/dashboard/handler.py's timeseries reader.
    """
    names = tuple(names)
    if not names:
        # `IN ()` is a syntax error; `IN (NULL)` is valid and matches nothing.
        return "IN (NULL)", {}
    placeholders = ", ".join(f":{prefix}{i}" for i in range(len(names)))
    params = {f"{prefix}{i}": name for i, name in enumerate(names)}
    return f"IN ({placeholders})", params
