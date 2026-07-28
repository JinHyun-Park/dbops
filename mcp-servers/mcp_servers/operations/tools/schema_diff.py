"""schema_diff: structured diff between two schema_snapshots rows.

The diff computation itself lives in operations/schema_diff_util.py because the
COLLECTOR has to produce diff_from_previous_json in the exact same bucket shape;
otherwise get_schema_diff and get_schema_history describe the same DDL event two
different ways. Only the SQL and the honest-empty-state handling are here.

Two false negatives this file used to have, both of which read to a DBA as
"nobody touched the schema":
  * a cluster with exactly ONE snapshot produced no rn=2 row, so the join was
    empty and the answer was `schemas_compared: 0` with all totals zero. One
    snapshot is a baseline, not a history: it is honestly NOT COMPARABLE. The
    implicit query is now a LEFT JOIN off the latest row, so a baseline-only
    schema still comes back and is reported as such per schema.
  * a cluster with NO snapshots at all was indistinguishable from the above.
    That is now `status: "not_collected"`.
"""

from mcp_servers.operations.schema_diff_util import compute_diff, parse_tables
from mcp_servers.shared.cache_client import CacheClient

# Re-exported under the old private names: the existing unit tests and any
# in-repo caller import them from this module.
_parse_tables = parse_tables
_compute_diff = compute_diff

# LEAST/GREATEST, not a straight bind of each argument to its own side. compute_diff
# infers `added` and `dropped` from set difference, so binding whichever timestamp
# the caller happened to put first as the BEFORE means calling this with the
# arguments the other way round reports a CREATE as a DROP, with status ok and
# nothing in the payload to hint at it. Nothing tells the agent which argument is
# the earlier one, so the SQL decides: `a` is always the chronologically earlier
# snapshot. The returned snapshot_before / snapshot_after make the direction
# explicit in the payload rather than implicit in the argument order.
EXPLICIT_SQL = (
    "SELECT a.schema_name, a.tables_json AS tables_before, "
    "       b.tables_json AS tables_after, "
    "       a.snapshot_time::text AS snapshot_before, "
    "       b.snapshot_time::text AS snapshot_after "
    "FROM schema_snapshots a, schema_snapshots b "
    "WHERE a.cluster_id = :cluster_id AND b.cluster_id = :cluster_id "
    "  AND a.snapshot_time = LEAST(:snapshot_a::timestamptz, :snapshot_b::timestamptz) "
    "  AND b.snapshot_time = GREATEST(:snapshot_a::timestamptz, :snapshot_b::timestamptz) "
    "  AND a.schema_name = b.schema_name "
    "ORDER BY a.schema_name"
)

# LEFT JOIN, not JOIN: the rn=1 (latest) row is the driver and the rn=2 row is
# optional, so a schema whose only snapshot is its baseline still appears with a
# NULL tables_before. An inner join silently dropped it and the tool then
# reported zero differences for a cluster it had never been able to compare.
#
# Both snapshot_times come back because the producer is STORE-ON-CHANGE: the
# implicit latest-vs-previous diff is the most recent DDL event whenever it
# happened, which can be months ago. Without a date in the payload the agent has
# nothing to date it by and presents it as if it just happened.
IMPLICIT_SQL = (
    "WITH ranked AS ( "
    "  SELECT cluster_id, schema_name, snapshot_time, tables_json, "
    "         ROW_NUMBER() OVER (PARTITION BY schema_name ORDER BY snapshot_time DESC) AS rn "
    "  FROM schema_snapshots "
    "  WHERE cluster_id = :cluster_id"
    ") "
    "SELECT b.schema_name, a.tables_json AS tables_before, "
    "       b.tables_json AS tables_after, "
    "       a.snapshot_time::text AS snapshot_before, "
    "       b.snapshot_time::text AS snapshot_after "
    "FROM ranked b LEFT JOIN ranked a "
    "  ON a.schema_name = b.schema_name AND a.rn = 2 "
    "WHERE b.rn = 1 "
    "ORDER BY b.schema_name"
)

COVERAGE_SQL = (
    "SELECT COUNT(*) AS snapshots, COUNT(DISTINCT schema_name) AS schemas, "
    "       MIN(snapshot_time)::text AS first_seen, MAX(snapshot_time)::text AS last_seen "
    "FROM schema_snapshots WHERE cluster_id = :cluster_id"
)

_NOT_COLLECTED = (
    "이 클러스터의 스키마 스냅샷이 아직 수집되지 않아 비교할 대상이 없습니다. "
    "스키마 차이가 없다는 뜻이 아닙니다. 다음 ETL 수집 주기(기본 5분 간격)에 "
    "최초 baseline 스냅샷이 기록되고, 그 다음 변경 시점부터 diff가 만들어집니다."
)
_BASELINE_ONLY = (
    "baseline 스냅샷만 있어 비교 대상이 없습니다 (diff에는 최소 2개가 필요합니다). "
    "다음 스키마 변경이 감지되면 그 시점의 스냅샷과 비교됩니다."
)


def _coverage(cache: CacheClient, cluster_id: str) -> dict:
    rows = cache.execute(COVERAGE_SQL, {"cluster_id": cluster_id}).rows
    cov = rows[0] if rows else {}
    return {
        "snapshots_stored": int(cov.get("snapshots") or 0),
        "schemas_tracked": int(cov.get("schemas") or 0),
        "first_snapshot": cov.get("first_seen"),
        "last_snapshot": cov.get("last_seen"),
    }


def get_schema_diff_impl(
    cache: CacheClient,
    cluster_id: str,
    snapshot_a: str = None,
    snapshot_b: str = None,
) -> dict:
    """Return structured diff between two snapshots.

    With both snapshots given: explicit diff between A and B.
    Without: diff between the latest snapshot and the one before it.
    """
    explicit = bool(snapshot_a and snapshot_b)
    if explicit:
        sql = EXPLICIT_SQL
        params = {
            "cluster_id": cluster_id,
            "snapshot_a": snapshot_a,
            "snapshot_b": snapshot_b,
        }
    else:
        sql = IMPLICIT_SQL
        params = {"cluster_id": cluster_id}

    result = cache.execute(sql, params)

    diffs: list[dict] = []
    baseline_only: list[str] = []
    totals = {"added": 0, "dropped": 0, "modified": 0, "rename_candidates": 0}
    for row in result.rows:
        before_blob = row.get("tables_before")
        if not explicit and not before_blob:
            # Latest row with no rn=2 partner: this schema has a baseline and
            # nothing to compare it against. Emitting a diff here would report
            # every existing table as newly added.
            baseline_only.append(row.get("schema_name"))
            continue
        diff = compute_diff(parse_tables(before_blob), parse_tables(row.get("tables_after")))
        for k in totals:
            totals[k] += len(diff[k])
        diffs.append({
            "schema_name": row.get("schema_name"),
            # WHEN the change was recorded, per schema. Store-on-change means the
            # newest pair is the newest DDL EVENT, not a recent one: a diff dated
            # months ago is normal and the agent has to be able to see that.
            "snapshot_time": row.get("snapshot_after"),
            "previous_snapshot_time": row.get("snapshot_before"),
            **diff,
        })

    coverage = _coverage(cache, cluster_id)
    out = {
        "status": "ok",
        "cluster_id": cluster_id,
        "schemas_compared": len(diffs),
        "totals": totals,
        "diffs": diffs,
        "collection_coverage": coverage,
    }
    if baseline_only:
        out["baseline_only_schemas"] = baseline_only

    if diffs:
        latest = max((d["snapshot_time"] for d in diffs if d["snapshot_time"]),
                     default=None)
        out["note"] = (
            f"비교된 스냅샷 시각은 각 diff의 previous_snapshot_time -> snapshot_time"
            f"입니다 (가장 최근: {latest}). 스냅샷은 스키마 변경이 감지된 시점에만 "
            "기록되므로, 이 diff가 최근에 발생한 변경이라는 뜻은 아닙니다."
        )
        if explicit:
            out["note"] += (
                " 요청한 두 시각은 시간순으로 정규화해 비교했습니다 (이른 쪽이 before)."
            )
        return out

    # Nothing compared. Say WHY, because "no differences" and "never collected"
    # lead a DBA to opposite conclusions.
    #
    # `explicit` is tested BEFORE the baseline-only branch. A caller who passed
    # two timestamps that matched no pair was being told "only a baseline exists"
    # whenever the cluster happened to have at most one snapshot per schema,
    # which answers a question they did not ask and drops the coverage range that
    # would have named the snapshots that DO exist.
    if coverage["snapshots_stored"] == 0:
        out["status"] = "not_collected"
        out["note"] = _NOT_COLLECTED
    elif explicit:
        out["status"] = "snapshots_not_found"
        out["note"] = (
            "요청한 두 시각과 정확히 일치하는 스냅샷 쌍을 찾지 못했습니다 "
            "(snapshot_time 완전 일치가 필요합니다). get_schema_history로 실제 "
            f"스냅샷 시각을 먼저 확인하세요. 수집 구간: {coverage['first_snapshot']} ~ "
            f"{coverage['last_snapshot']}."
        )
    elif baseline_only or coverage["snapshots_stored"] <= coverage["schemas_tracked"]:
        out["status"] = "insufficient_snapshots"
        out["note"] = _BASELINE_ONLY
    else:
        out["status"] = "no_changes"
        out["note"] = (
            f"최신 스냅샷과 그 이전 스냅샷 사이에 스키마 차이가 없습니다 "
            f"(스냅샷 {coverage['snapshots_stored']}건, 최근 수집: "
            f"{coverage['last_snapshot']})."
        )
    return out
