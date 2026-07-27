import os

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.engine_family import DOCUMENTDB, RDS_INSTANCE, engine_family

_ORDER = """
ORDER BY
    CASE severity
        WHEN 'critical' THEN 0
        WHEN 'warning'  THEN 1
        ELSE 2
    END,
    check_type
"""

_COLS = "check_type, severity, subject, value_str, threshold_str, recommendation, details"

# Single-writer families: every finding of one cycle lands on one shared run_ts,
# so the newest snapshot IS the complete current picture (and a check_type the
# next cycle stops emitting resolves immediately).
_SQL_LATEST_SNAPSHOT = f"""
SELECT {_COLS}
FROM cluster_health_findings
WHERE cluster_id = :cid
  AND snapshot_time = (
      SELECT MAX(snapshot_time)
      FROM cluster_health_findings
      WHERE cluster_id = :cid
  )
{_ORDER}
"""

# Families with TWO writer Lambdas on independent schedules and DISJOINT
# check_type sets:
#   rds_instance: etl_collector (cost / capacity_forecast / param_fitness /
#                  query_regression) + rds_direct_collector (InnoDB status)
#   documentdb:   etl_collector docdb_findings (connection_saturation /
#                  cost_oversized / cursor_timeout / low_cache_hit / replica_lag)
#                  + docdb_mongo_collector (docdb_mongo_long_running_ops)
# One global MAX(snapshot_time) returns only whichever Lambda wrote last, so the
# agent would tell the DBA half the truth. Take the latest snapshot PER
# check_type inside a freshness window instead.
_MULTI_WRITER_FAMILIES = frozenset({RDS_INSTANCE, DOCUMENTDB})

# The window must be >= the longest writer interval of those families, so it is
# derived from the writers' REAL cadence, never from a hardcoded assumption about
# it: FINDINGS_WRITER_INTERVAL_MIN is the ETL findings collector's schedule
# (Settings.STATS_COLLECTION_INTERVAL_MIN, wired by cdk/stacks/agent_stack.py).
# That setting is per-deployment and gitignored, so a deployer who raises it to
# save cost would otherwise silently hide half of every rds_instance / documentdb
# cluster's findings again. 3x the cadence lets one writer miss two consecutive
# runs without its findings vanishing. The floor is 3x the 5-minute rate that
# rds_direct_collector and docdb_mongo_collector are pinned to in data_stack, so
# an unset or garbage env var can only WIDEN the window, never shrink it.
# Same derivation in api/dashboard/handler.py (api/ cannot import mcp_servers);
# pinned by a parity test.
#
# The window is measured from the cluster's OWN newest finding, NOT from NOW():
# api/clusters/seeder.py writes the demo cluster's findings once at seed time and
# never re-emits them, so a NOW()-relative window would report a seeded cluster
# as having no maintenance issues at all.
_WINDOW_FLOOR_MIN = 15


def _window_min():
    try:
        cadence = int(os.environ.get("FINDINGS_WRITER_INTERVAL_MIN", ""))
    except ValueError:
        return _WINDOW_FLOOR_MIN
    return max(_WINDOW_FLOOR_MIN, cadence * 3)


def _sql_per_check_type():
    return f"""
SELECT {_COLS}
FROM (
    SELECT {_COLS}, snapshot_time,
           MAX(snapshot_time) OVER (PARTITION BY check_type) AS ct_latest
    FROM cluster_health_findings
    WHERE cluster_id = :cid
      AND snapshot_time >= (
          SELECT MAX(snapshot_time)
          FROM cluster_health_findings
          WHERE cluster_id = :cid
      ) - INTERVAL '{_window_min()} minutes'
) ranked
WHERE snapshot_time = ct_latest
{_ORDER}
"""

_UNRESOLVED_REASON = (
    "클러스터 엔진을 확인할 수 없어 유지보수 findings를 조회하지 않았습니다. "
    "등록되지 않은 클러스터이거나 첫 수집 전일 수 있습니다. 이 응답은 "
    "'이슈 없음'이 아닙니다. 클러스터 등록·수집 상태를 확인한 뒤 다시 시도하세요."
)


def _resolve_family(cache: CacheClient, cluster_id: str):
    """Engine family from cluster_meta, or None when it cannot be resolved
    (no cluster_id, no row, or the lookup failed). Mirrors the per-server
    handler gate; None is FAIL-CLOSED here, never 'assume relational'."""
    if not cluster_id:
        return None
    try:
        result = cache.execute(
            "SELECT engine FROM cluster_meta WHERE cluster_id = :cid",
            {"cid": cluster_id},
        )
    except Exception:
        # Detail goes to CloudWatch only, never into the agent-visible payload.
        print(f"[incident] family lookup failed for {cluster_id}")
        return None
    rows = getattr(result, "rows", result)
    if isinstance(rows, list) and rows and isinstance(rows[0], dict) and rows[0].get("engine"):
        return engine_family(rows[0]["engine"])
    return None


def get_maintenance_findings_impl(cache: CacheClient, cluster_id: str) -> dict:
    # FAIL-CLOSED: unlike the dashboard (where an empty panel is just blank),
    # an empty findings list here reads to the agent as "no maintenance issues"
    # and it will say so to the DBA. So an unresolvable cluster or a failed
    # query returns an explicit error status with NO findings key.
    fam = _resolve_family(cache, cluster_id)
    if fam is None:
        return {
            "status": "error",
            "cluster_id": cluster_id,
            "reason": _UNRESOLVED_REASON,
        }

    sql = _sql_per_check_type() if fam in _MULTI_WRITER_FAMILIES else _SQL_LATEST_SNAPSHOT
    try:
        result = cache.execute(sql, {"cid": cluster_id})
    except Exception:
        print(f"[incident] findings query failed for {cluster_id}")
        return {
            "status": "error",
            "cluster_id": cluster_id,
            "reason": (
                "유지보수 findings 조회에 실패했습니다. 이 응답은 '이슈 없음'이 아닙니다. "
                "잠시 후 다시 시도하세요."
            ),
        }

    counts = {"critical": 0, "warning": 0, "info": 0}
    findings = []

    for row in getattr(result, "rows", result) or []:
        sev = row.get("severity", "info")
        if sev in counts:
            counts[sev] += 1
        else:
            counts["info"] += 1

        findings.append({
            "check_type": row.get("check_type"),
            "severity": sev,
            "subject": row.get("subject"),
            "value_str": row.get("value_str"),
            "threshold_str": row.get("threshold_str"),
            "recommendation": row.get("recommendation"),
        })

    return {
        "status": "ok",
        "cluster_id": cluster_id,
        "engine_family": fam,
        "findings": findings,
        "counts": counts,
    }
