"""get_schema_history: replay the stored schema_snapshots diffs for a window.

This tool used to answer `count: 0` for an empty result, which conflates two
opposite facts: "we have been snapshotting this cluster and its schema did not
change" versus "nothing has ever been collected here, so we do not know". A DBA
asked "did anyone change the schema before the incident?" acts on those two
answers in opposite directions, so the empty result is now qualified by a
COLLECTION-COVERAGE probe and reported as an explicit status.
"""

from mcp_servers.shared.cache_client import CacheClient

CHANGES_SQL = """
    SELECT snapshot_time, schema_name, diff_from_previous_json as changes
    FROM schema_snapshots
    WHERE cluster_id = :cluster_id AND snapshot_time > NOW() - (:days || ' days')::interval
      AND diff_from_previous_json IS NOT NULL AND diff_from_previous_json != '{}'
    ORDER BY snapshot_time DESC
"""

# Coverage probe: does a PRODUCER exist for this cluster at all? Runs only on the
# empty path, so the happy path stays a single query.
COVERAGE_SQL = """
    SELECT COUNT(*) AS snapshots,
           COUNT(DISTINCT schema_name) AS schemas,
           MIN(snapshot_time)::text AS first_seen,
           MAX(snapshot_time)::text AS last_seen
    FROM schema_snapshots
    WHERE cluster_id = :cluster_id
"""

_NOT_COLLECTED = (
    "이 클러스터의 스키마 스냅샷이 아직 수집되지 않았습니다. "
    "스키마가 변경되지 않았다는 뜻이 아니라, 비교할 기록 자체가 없다는 뜻입니다. "
    "다음 ETL 수집 주기(기본 5분 간격)에 최초 baseline 스냅샷이 기록됩니다."
)
_BASELINE_ONLY = (
    "baseline 스냅샷 1개만 수집된 상태입니다. 변경 이력은 두 번째 스냅샷이 "
    "기록되는 시점부터 생성되므로, 현재는 변경 여부를 판단할 수 없습니다."
)


def get_schema_history_impl(cache: CacheClient, cluster_id: str, days: int = 30) -> dict:
    params = {"cluster_id": cluster_id, "days": days}
    result = cache.execute(CHANGES_SQL, params)
    if result.rows:
        return {
            "status": "ok",
            "cluster_id": cluster_id,
            "period_days": days,
            "changes": result.rows,
            "count": result.row_count,
        }

    # Empty window. Before saying anything that sounds like "nothing changed",
    # find out whether we have any data at all for this cluster.
    cov_rows = cache.execute(COVERAGE_SQL, {"cluster_id": cluster_id}).rows
    cov = cov_rows[0] if cov_rows else {}
    snapshots = int(cov.get("snapshots") or 0)
    coverage = {
        "snapshots_stored": snapshots,
        "schemas_tracked": int(cov.get("schemas") or 0),
        "first_snapshot": cov.get("first_seen"),
        "last_snapshot": cov.get("last_seen"),
    }

    if snapshots == 0:
        status, note = "not_collected", _NOT_COLLECTED
    elif snapshots <= coverage["schemas_tracked"]:
        # Comparability is PER SCHEMA: a cluster with 3 schemas and one baseline
        # each has 3 rows and still nothing to diff. `snapshots > schemas` holds
        # exactly when at least one schema has a second snapshot.
        status, note = "baseline_only", _BASELINE_ONLY
    else:
        status, note = "no_changes", (
            f"수집된 스냅샷 {snapshots}건 범위에서 최근 {days}일간 스키마 변경이 없습니다 "
            f"(수집 구간: {coverage['first_snapshot']} ~ {coverage['last_snapshot']})."
        )

    return {
        "status": status,
        "cluster_id": cluster_id,
        "period_days": days,
        "changes": [],
        "count": 0,
        "collection_coverage": coverage,
        "note": note,
    }
