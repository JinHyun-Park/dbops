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

A THIRD false negative, from the producer side: a schema the collector can no
longer SEE has no new snapshot, so it fell in with the unchanged schemas and this
tool answered "no differences" for it. The collector used to paper over that by
inferring a DROP from absence, which produced a phantom mass drop (see
data-pipeline/etl_collector/collectors/schema_snapshot.py). It now records the
absence as nothing at all, so this tool has to report it: `observation` names the
schemas the newest read did not confirm, and `status` is `partial`, not
`no_changes`, whenever a negative cannot cover the whole cluster.
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

# ---------------------------------------------------------------------------
# WAS THE SCHEMA STILL THERE, and is that a different question from WHEN IT LAST
# CHANGED? Yes, and conflating the two is the defect five passes over
# data-pipeline/etl_collector/collectors/schema_snapshot.py kept relocating.
#
# The producer is STORE-ON-CHANGE, so snapshot_time is when a schema last
# CHANGED: months ago for a stable schema. That says nothing about whether the
# collector can still SEE it. The collector used to close the gap by inferring
# "absent from my catalog read, therefore dropped", which reported a phantom mass
# DROP whenever a read landed in the wrong database. It no longer infers anything:
# a schema it does not see records NOTHING, and schema_v27's last_seen_at carries
# the fact it can support ("still there as of T, under scope S").
#
# So the honest answer here has THREE parts, not two: what changed, what did not
# change, and WHAT WAS NOT LOOKED AT. This probe is the third one. Without it a
# schema nobody can see any more reads as an unchanged schema, which is the same
# false negative in the opposite direction.
#
# Three uncorrelated scalar subqueries: PostgreSQL evaluates each once as an
# InitPlan, so this costs one extra index scan per call, not one per schema.
OBSERVATION_SQL = (
    "SELECT schema_name, last_seen_at::text AS last_seen, "
    "       CASE WHEN tables_json <> '{}'::jsonb THEN 'y' ELSE 'n' END AS holds_tables, "
    "       CASE WHEN last_seen_at = (SELECT MAX(y.last_seen_at) FROM schema_snapshots y "
    "                                WHERE y.cluster_id = :cluster_id) "
    "            THEN 'y' ELSE 'n' END AS confirmed_now, "
    "       (SELECT MAX(y.last_seen_at)::text FROM schema_snapshots y "
    "        WHERE y.cluster_id = :cluster_id) AS last_confirmed, "
    "       (SELECT EXTRACT(EPOCH FROM (NOW() - MAX(y.last_seen_at)))::bigint "
    "        FROM schema_snapshots y WHERE y.cluster_id = :cluster_id) AS age_sec "
    "FROM ("
    "  SELECT DISTINCT ON (schema_name) schema_name, last_seen_at, tables_json, snapshot_time "
    "  FROM schema_snapshots WHERE cluster_id = :cluster_id "
    "  ORDER BY schema_name, snapshot_time DESC"
    ") latest ORDER BY schema_name"
)

# Same bar the dashboard's own freshness indicator uses (api/dashboard/handler.py
# _FRESH_MAX_AGE_SEC), which is 3 ETL cycles at the default 5-minute interval.
_OBSERVATION_MAX_AGE_SEC = 15 * 60

# A `dropped` list is only ever as good as the catalog the collector could read.
# On MySQL that catalog is privilege-filtered, so a table-level REVOKE is
# byte-identical to a DROP TABLE and there is no probe that separates them. It is
# disclosed here rather than resolved, because resolving it would mean picking one
# of two answers the data does not choose between.
DROPPED_CAVEAT = (
    "dropped 목록은 수집 계정이 그 시점에 볼 수 있었던 카탈로그를 기준으로 계산됩니다. "
    "MySQL은 information_schema가 권한 필터링되므로 테이블 권한 회수(REVOKE)가 DROP과 "
    "같은 모양으로 보입니다."
)


def observation_state(cache: CacheClient, cluster_id: str) -> dict:
    """When each schema was last OBSERVED to exist, as opposed to last changed.

    status:
      fresh          the newest observation is inside _OBSERVATION_MAX_AGE_SEC
      stale          snapshots exist but nothing has been confirmed recently: the
                     collector is not running for this cluster, or every read is
                     landing outside the scope the history was recorded from
      unknown        rows exist but none carries an observation (all of them
                     predate schema_v27, or the first cycle since it has not run)
      no_snapshots   nothing stored for this cluster at all
      unavailable    the probe itself could not run (a cache DB without
                     schema_v27). Named, never swallowed.
    unconfirmed_schemas: schemas whose stored snapshot still serves tables and
    which the newest read did NOT confirm. Each one is an UNKNOWN: a DROP SCHEMA
    and a read that could not reach it leave identical evidence.
    """
    try:
        rows = cache.execute(OBSERVATION_SQL, {"cluster_id": cluster_id}).rows
    except Exception as e:
        # Detail to CloudWatch only: no exception text may reach a response.
        print(f"[schema] observation probe unavailable: {type(e).__name__}: {e}")
        return {"status": "unavailable"}
    if not rows:
        return {"status": "no_snapshots"}
    last_confirmed = rows[0].get("last_confirmed")
    age = rows[0].get("age_sec")
    age = int(age) if age is not None else None
    if last_confirmed is None:
        status = "unknown"
    elif age is not None and age > _OBSERVATION_MAX_AGE_SEC:
        status = "stale"
    else:
        status = "fresh"
    return {
        "status": status,
        "last_confirmed": last_confirmed,
        "age_sec": age,
        "unconfirmed_schemas": sorted(
            r.get("schema_name") for r in rows
            if r.get("holds_tables") == "y" and r.get("confirmed_now") != "y"
        ),
    }


def observation_is_complete(obs: dict) -> bool:
    """True only when every schema this cluster stores tables for was confirmed by
    the newest read. Anything else means part of the question was not looked at,
    so a negative answer cannot cover the whole cluster."""
    return obs.get("status") == "fresh" and not obs.get("unconfirmed_schemas")


def observation_note(obs: dict) -> str:
    """Korean prose for whatever the observation cannot support. Empty when the
    cluster was fully confirmed.

    ADDITIVE, not a first-match ladder: cluster-level staleness and a named
    unconfirmed schema are two different unknowns and a stale cluster can have
    both, so reporting only the first drops the schema NAME the DBA needs.
    """
    parts = []
    status = obs.get("status")
    if status == "unavailable":
        parts.append("스키마 관측 기록을 조회할 수 없어(schema_v27 미적용 캐시 DB) 각 "
                     "스키마가 현재도 존재하는지는 확인하지 못했습니다.")
    elif status == "unknown":
        parts.append("저장된 스냅샷에 관측 기록이 아직 없습니다. 다음 수집 주기부터 각 "
                     "스키마의 마지막 확인 시각이 기록됩니다.")
    elif status == "stale":
        parts.append(f"이 클러스터의 스키마 관측이 {obs.get('last_confirmed')} 이후 "
                     "갱신되지 않았습니다. 수집이 멈췄거나, 읽기가 기록 당시와 다른 "
                     "데이터베이스를 보고 있을 수 있습니다. 아래 내용은 그 시점까지의 "
                     "기록입니다.")
    unconfirmed = obs.get("unconfirmed_schemas") or []
    if unconfirmed:
        parts.append(f"{', '.join(unconfirmed)} 스키마는 최근 카탈로그 읽기에서 확인되지 "
                     "않았습니다. 삭제되었을 수도 있고 읽기가 도달하지 못한 것일 수도 "
                     "있어, 삭제로 단정하지 않고 '확인 불가'로 보고합니다. 이 스키마의 "
                     "테이블 목록은 마지막 확인 시점의 상태입니다.")
    return " ".join(parts)

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
    observation = observation_state(cache, cluster_id)
    obs_note = observation_note(observation)
    out = {
        "status": "ok",
        "cluster_id": cluster_id,
        "schemas_compared": len(diffs),
        "totals": totals,
        "diffs": diffs,
        "collection_coverage": coverage,
        # WHAT WAS NOT LOOKED AT. A schema missing from `diffs` is either
        # unchanged or unseen, and those are opposite facts for a DBA.
        "observation": observation,
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
        if totals["dropped"]:
            out["note"] += " " + DROPPED_CAVEAT
        if obs_note:
            out["note"] += " " + obs_note
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
    elif not observation_is_complete(observation):
        # A real negative requires that every schema was actually looked at. With
        # a schema left unconfirmed, "no differences" is true only of the part of
        # the cluster the read reached, and the same sentence over the whole
        # cluster is a negative the data does not support.
        out["status"] = "partial"
        out["note"] = (
            f"읽어온 스냅샷 사이에서는 스키마 차이가 없었습니다 (스냅샷 "
            f"{coverage['snapshots_stored']}건, 최근 수집: {coverage['last_snapshot']}). "
            "다만 클러스터 전체에 대해 '변경 없음'이라고 말할 수는 없습니다. "
        ) + obs_note
    else:
        out["status"] = "no_changes"
        out["note"] = (
            f"최신 스냅샷과 그 이전 스냅샷 사이에 스키마 차이가 없습니다 "
            f"(스냅샷 {coverage['snapshots_stored']}건, 최근 수집: "
            f"{coverage['last_snapshot']}, 모든 스키마가 "
            f"{observation.get('last_confirmed')}에 확인됨)."
        )
    # EVERY status, not just the negative one: a schema nobody can see any more is
    # news on the not_collected / baseline / explicit-miss paths too, and putting
    # it only where it changed the status is how a state ends up living in one
    # branch instead of in the answer.
    if obs_note and obs_note not in out["note"]:
        out["note"] += " " + obs_note
    return out
