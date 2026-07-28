"""get_schema_history: replay the stored schema_snapshots diffs for a window.

This tool used to answer `count: 0` for an empty result, which conflates two
opposite facts: "we have been snapshotting this cluster and its schema did not
change" versus "nothing has ever been collected here, so we do not know". A DBA
asked "did anyone change the schema before the incident?" acts on those two
answers in opposite directions, so the empty result is qualified by a
COLLECTION-COVERAGE probe and reported as an explicit status.

Coverage is not the whole of it. A schema the collector can no longer SEE files no
row either, so it used to sit inside the same empty result as a genuinely
unchanged schema. Absence is never resolved to a DROP (it produced a phantom mass
drop; see mcp_servers/shared/schema_diff_util.py), so this tool carries the
unknown instead: `observation` names the schemas the newest scope-matching read did
not confirm, and an empty window with one of those is `partial`, never
`no_changes`.

REPLAY, NOT RECOMPUTE, which is why this file reads ALL_ROWS and not SCOPED_ROWS.
Each stored diff_from_previous_json was computed by the producer against a
same-scope predecessor by construction, so replaying it is both safe and complete.
Filtering the replay by the CURRENT scope would erase real DDL history from the
record every time a cluster is re-scoped. The contract names the two row sources
separately (mcp_servers/shared/schema_diff_util.py) so that difference is a
decision made once rather than a `FROM schema_snapshots` written per consumer.
"""

from mcp_servers.operations.tools.schema_diff import (
    observation_note,
    observation_state,
)
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.schema_diff_util import (
    ALL_ROWS,
    COVERAGE_SQL,
    DROPPED_CAVEAT,
    observation_is_complete,
)

CHANGES_SQL = (
    "SELECT snapshot_time, schema_name, read_scope, "
    "       diff_from_previous_json as changes "
    + ALL_ROWS +
    "  AND snapshot_time > NOW() - (:days || ' days')::interval "
    "  AND diff_from_previous_json IS NOT NULL "
    "  AND diff_from_previous_json != '{}' "
    "ORDER BY snapshot_time DESC"
)

# Coverage probe: does a PRODUCER exist for this cluster at all? Runs only on the
# empty path, so the happy path stays a single query.
_COVERAGE_SQL = COVERAGE_SQL

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
    observation = observation_state(cache, cluster_id)
    obs_note = observation_note(observation)
    if result.rows:
        return {
            "status": "ok",
            "cluster_id": cluster_id,
            "period_days": days,
            "changes": result.rows,
            "count": result.row_count,
            "observation": observation,
            # Every row here is a change list that can contain dropped tables, so
            # the caveat rides along unconditionally rather than being inferred
            # from parsing each stored diff.
            "note": (DROPPED_CAVEAT + (" " + obs_note if obs_note else "")),
        }

    # Empty window. Before saying anything that sounds like "nothing changed",
    # find out whether we have any data at all for this cluster.
    cov_rows = cache.execute(_COVERAGE_SQL, {"cluster_id": cluster_id}).rows
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
    elif not observation_is_complete(observation):
        # An empty window is a real negative only over the schemas that were
        # actually looked at. One unconfirmed schema (or a cluster whose history is
        # not comparable at all) makes the cluster-wide sentence unsupportable.
        status, note = "partial", (
            f"읽어온 스냅샷 {snapshots}건 범위에서 최근 {days}일간 변경 기록이 "
            f"없었습니다 (수집 구간: {coverage['first_snapshot']} ~ "
            f"{coverage['last_snapshot']}). 다만 클러스터 전체에 대해 '변경 없음'이라고 "
            "말할 수는 없습니다. "
        ) + obs_note
    else:
        status, note = "no_changes", (
            f"수집된 스냅샷 {snapshots}건 범위에서 최근 {days}일간 스키마 변경이 없습니다 "
            f"(수집 구간: {coverage['first_snapshot']} ~ {coverage['last_snapshot']}, "
            f"모든 스키마가 {observation.get('last_confirmed')}에 확인됨)."
        )

    # EVERY status, not just the negative one: a schema nobody can see any more is
    # news on the not_collected and baseline_only paths too.
    if obs_note and obs_note not in note:
        note += " " + obs_note

    return {
        "status": status,
        "cluster_id": cluster_id,
        "period_days": days,
        "changes": [],
        "count": 0,
        "collection_coverage": coverage,
        "observation": observation,
        "note": note,
    }
