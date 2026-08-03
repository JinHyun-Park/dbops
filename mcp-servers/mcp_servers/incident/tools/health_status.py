"""get_health_status: control-plane state PLUS whether we can actually see the cluster.

TWO FAILURES THIS FILE USED TO HAVE, in opposite directions, from one cause
---------------------------------------------------------------------------
`health` was derived from a hardcoded lookup on `cluster_meta.status` with a
`critical` catch-all, and nothing else. Measured 2026-08-02 by calling the tool
against 9 real clusters, one per engine family:

  1. A CLUSTER WITH NO TELEMETRY REPORTED "healthy". `status == "available"` is a
     control-plane fact that stays true while collection is broken, so a monitoring
     outage was indistinguishable from a healthy cluster, with `current_metrics: []`
     sitting right there and nothing saying so. That is the one case this tool
     exists for.
  2. A HEALTHY DYNAMODB TABLE REPORTED "critical". DynamoDB's TableStatus word is
     `ACTIVE`, not `available`, so it missed the healthy branch and fell through the
     catch-all. A false critical is worse than a missed one: it trains the reader to
     ignore the field.

So the status vocabulary is now per-engine and explicit, and an UNRECOGNISED word
is `unknown` rather than `critical`. Guessing "critical" from a word we do not know
is the same class of mistake as guessing "healthy" from a word we do.

The vocabularies are the real AWS ones, not invented:
  RDS / Aurora / DocumentDB DBInstanceStatus, DBClusterStatus
  DynamoDB TableStatus
  ElastiCache ReplicationGroup Status
Case is normalised because DynamoDB shouts and RDS does not.
"""

import json

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.metric_filters import CLUSTER_LEVEL_ONLY

# Engines that are NOT relational Aurora clusters — for these we surface
# engine + parsed resource_details so the agent has billing mode, capacity,
# GSI/LSI counts, instance topology, etc. without a separate lookup.
_NON_RELATIONAL_ENGINES = {"dynamodb", "docdb", "documentdb"}

# The window the metric aggregate below covers. Returned to the caller so an empty
# result is readable as "nothing in the last N minutes" rather than "nothing".
_METRICS_WINDOW_MINUTES = 10

# Steady, serving states across the engine families.
_HEALTHY_STATUSES = {
    "available",   # RDS / Aurora / DocumentDB / ElastiCache
    "active",      # DynamoDB TableStatus
}

# In-progress states: the cluster is serving or about to, but something is moving.
_TRANSITIONAL_STATUSES = {
    "modifying", "backing-up", "snapshotting", "upgrading", "configuring-enhanced-monitoring",
    "creating", "updating", "renaming", "starting", "maintenance", "resetting-master-credentials",
    "converting-to-vpc", "moving-to-vpc", "storage-optimization",
}

# States that are genuinely bad. Kept explicit so an unfamiliar word does NOT land
# here by default; that catch-all is what made a healthy DynamoDB table critical.
_CRITICAL_STATUSES = {
    "failed", "create-failed", "restore-error", "incompatible-parameters",
    "incompatible-network", "incompatible-restore", "incompatible-credentials",
    "storage-full", "inaccessible-encryption-credentials",
    "inaccessible_encryption_credentials",  # DynamoDB's underscore form
    "stopped", "stopping", "deleting", "deleted", "archiving", "archived",
}


def _classify(status: str) -> tuple[str, str | None]:
    """(health, reason) from a control-plane status word alone.

    An unknown word yields "unknown" WITH the word echoed, so the fix is obvious
    (add it to one of the sets) instead of hiding behind a wrong verdict.
    """
    s = (status or "").strip().lower().replace("_", "-")
    if not s or s == "unknown":
        return "unknown", "cluster_meta에 status가 없습니다 (등록 직후이거나 수집 전일 수 있습니다)."
    if s in _HEALTHY_STATUSES:
        return "healthy", None
    if s in _TRANSITIONAL_STATUSES:
        return "warning", None
    if s in _CRITICAL_STATUSES:
        return "critical", None
    return "unknown", (
        f"control-plane status '{status}'를 이 엔진의 정상/전이/장애 어휘 어디에도 "
        f"매칭할 수 없습니다. 판단을 추측하지 않고 unknown으로 보고합니다."
    )


def get_health_status_impl(cache: CacheClient, cluster_id: str) -> dict:
    meta_sql = "SELECT * FROM cluster_meta WHERE cluster_id = :cluster_id"
    meta = cache.execute(meta_sql, {"cluster_id": cluster_id})

    metrics_sql = f"""
        SELECT metric_type, AVG(value) as avg_val, MAX(value) as max_val
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id
          AND ts > NOW() - INTERVAL '{_METRICS_WINDOW_MINUTES} minutes'
          {CLUSTER_LEVEL_ONLY}
        GROUP BY metric_type
    """
    metrics = cache.execute(metrics_sql, {"cluster_id": cluster_id})

    cluster = meta.rows[0] if meta.rows else {}
    status = cluster.get("status", "unknown")
    health, reason = _classify(status)

    metric_rows = metrics.rows or []
    # A steady control-plane state proves the cluster EXISTS, not that it is well.
    # With no samples in the window there is nothing to assess, so do not upgrade
    # "the API says available" into "healthy". Only the healthy verdict is
    # downgraded: a critical status is a real signal and stands on its own, and a
    # transitional one already tells the reader something is moving.
    if health == "healthy" and not metric_rows:
        health = "unknown"
        reason = (
            f"control-plane status는 '{status}'지만 최근 {_METRICS_WINDOW_MINUTES}분간 "
            f"클러스터 레벨 지표 표본이 0개입니다. 수집이 멈춘 상태와 정상 상태를 "
            f"구분할 수 없으므로 healthy로 단정하지 않습니다. ETL 수집 상태를 "
            f"확인하세요(등록 직후라면 첫 수집까지 몇 분 걸립니다)."
        )

    result: dict = {
        "cluster_id": cluster_id,
        "health": health,
        "cluster": cluster,
        "current_metrics": metric_rows,
        # Provenance for the verdict above. `metrics_count == 0` with
        # `health == "unknown"` is the monitoring-outage signal.
        "telemetry": {
            "window_minutes": _METRICS_WINDOW_MINUTES,
            "metrics_count": len(metric_rows),
            "control_plane_status": status,
        },
    }
    if reason:
        result["reason"] = reason

    engine = cluster.get("engine", "")
    if engine in _NON_RELATIONAL_ENGINES:
        result["engine"] = engine
        raw_details = cluster.get("resource_details")
        if raw_details is not None:
            if isinstance(raw_details, str):
                try:
                    result["resource_details"] = json.loads(raw_details)
                except (json.JSONDecodeError, ValueError):
                    result["resource_details"] = raw_details
            else:
                result["resource_details"] = raw_details
        else:
            result["resource_details"] = None

    return result
