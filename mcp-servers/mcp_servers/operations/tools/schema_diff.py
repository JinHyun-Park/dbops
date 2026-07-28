"""get_schema_diff: structured diff between two schema_snapshots rows.

EVERY statement here is built from the fragments in
mcp_servers/shared/schema_diff_util.py, and the diff comes from `compare()`
rather than compute_diff, because that is what forces this tool and the dashboard
panel and the collector through ONE definition of which two blobs are comparable.
Six passes over this surface each fixed the consumers its file ownership included;
the selection SQL was duplicated per consumer, so the defect always survived
somewhere. See that module's docstring for the history and the mechanical test.

Three false negatives this file used to have, all of which read to a DBA as
"nobody touched the schema":
  * a cluster with exactly ONE snapshot produced no rn=2 row, so the join was
    empty and the answer was `schemas_compared: 0` with all totals zero. One
    snapshot is a baseline, not a history: it is honestly NOT COMPARABLE. The
    implicit query is a LEFT JOIN off the latest row, so a baseline-only schema
    still comes back and is reported as such per schema.
  * a cluster with NO snapshots at all was indistinguishable from the above. That
    is now `status: "not_collected"`.
  * a schema the collector can no longer SEE has no new snapshot, so it fell in
    with the unchanged schemas and was answered "no differences". Absence is never
    resolved to a DROP (that produced a phantom mass drop), so it is reported as
    the unknown it is: `observation` names the schemas the newest scope-matching
    read did not confirm, and any status that would read as an absence of change
    becomes `partial`.
"""

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.schema_diff_util import (
    COVERAGE_SQL,
    DROPPED_CAVEAT,
    SCOPED_ROWS,
    UNSUPPORTED_DIALECT_NOTE,
    UNSUPPORTED_ENGINE,
    compare,
    not_seen_note,
    observation_is_complete,
    observed,
    parse_tables,
)

# Re-exported under the old private names: the existing unit tests and any
# in-repo caller import them from this module.
_parse_tables = parse_tables

# LEAST/GREATEST, not a straight bind of each argument to its own side. The diff
# infers `added` and `dropped` from set difference, so binding whichever timestamp
# the caller happened to put first as the BEFORE means calling this with the
# arguments the other way round reports a CREATE as a DROP, with status ok and
# nothing in the payload to hint at it. Nothing tells the agent which argument is
# the earlier one, so the SQL decides: `a` is always the chronologically earlier
# snapshot. The returned snapshot_before / snapshot_after make the direction
# explicit in the payload rather than implicit in the argument order.
#
# ONE `scoped` CTE, not two references to the table: the pair has to come from a
# single scope-filtered row source or the self-join can pick one row from each of
# two catalogs, which is the phantom-DROP shape.
EXPLICIT_SQL = (
    "WITH scoped AS ("
    "  SELECT schema_name, snapshot_time, tables_json " + SCOPED_ROWS +
    ") "
    "SELECT a.schema_name, a.tables_json AS tables_before, "
    "       b.tables_json AS tables_after, "
    "       a.snapshot_time::text AS snapshot_before, "
    "       b.snapshot_time::text AS snapshot_after "
    "FROM scoped a, scoped b "
    "WHERE a.snapshot_time = LEAST(:snapshot_a::timestamptz, :snapshot_b::timestamptz) "
    "  AND b.snapshot_time = GREATEST(:snapshot_a::timestamptz, :snapshot_b::timestamptz) "
    "  AND a.schema_name = b.schema_name "
    "ORDER BY a.schema_name"
)

# LEFT JOIN, not JOIN: the rn=1 (latest) row is the driver and the rn=2 row is
# optional, so a schema whose only snapshot is its baseline still appears with a
# NULL tables_before. An inner join silently dropped it and the tool then reported
# zero differences for a cluster it had never been able to compare.
#
# Both snapshot_times come back because the producer is STORE-ON-CHANGE: the
# implicit latest-vs-previous diff is the most recent DDL event whenever it
# happened, which can be months ago. Without a date in the payload the agent has
# nothing to date it by and presents it as if it just happened.
IMPLICIT_SQL = (
    "WITH ranked AS ( "
    "  SELECT schema_name, snapshot_time, tables_json, "
    "         ROW_NUMBER() OVER (PARTITION BY schema_name ORDER BY snapshot_time DESC) AS rn "
    "  " + SCOPED_ROWS +
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

# Coverage counts EVERY row of EVERY scope on purpose: it answers "is there any
# history here at all", which is a different question from "what may be compared".
# Reported beside a negative so "nothing to compare" and "nothing changed" cannot
# read the same way. ALL_ROWS, not SCOPED_ROWS, and that is the whole reason the
# contract names two row sources instead of one.
_COVERAGE_SQL = COVERAGE_SQL

_NOT_COLLECTED = (
    "이 클러스터의 스키마 스냅샷이 아직 수집되지 않아 비교할 대상이 없습니다. "
    "스키마 차이가 없다는 뜻이 아닙니다. 다음 ETL 수집 주기(기본 5분 간격)에 "
    "최초 baseline 스냅샷이 기록되고, 그 다음 변경 시점부터 diff가 만들어집니다."
)
_BASELINE_ONLY = (
    "baseline 스냅샷만 있어 비교 대상이 없습니다 (diff에는 최소 2개가 필요합니다). "
    "다음 스키마 변경이 감지되면 그 시점의 스냅샷과 비교됩니다."
)
_NOT_COMPARABLE = (
    "저장된 스냅샷을 현재 수집 범위와 비교할 수 없어 diff를 만들지 못했습니다. "
    "변경이 없다는 뜻이 아닙니다."
)


def observation_state(cache: CacheClient, cluster_id: str) -> dict:
    """The shared per-schema confirmation state. Kept as a named function here
    because get_schema_history imports it, and because the previous pass's
    cluster-wide-MAX version lived in this file and has to be gone from it."""
    return observed(lambda sql, params: cache.execute(sql, params).rows, cluster_id)


def observation_note(obs: dict) -> str:
    return not_seen_note(obs)


def _coverage(cache: CacheClient, cluster_id: str) -> dict:
    rows = cache.execute(_COVERAGE_SQL, {"cluster_id": cluster_id}).rows
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
    observation = observation_state(cache, cluster_id)
    obs_note = observation_note(observation)
    scope = observation.get("read_scope")

    diffs: list[dict] = []
    baseline_only: list[str] = []
    totals = {"added": 0, "dropped": 0, "modified": 0, "rename_candidates": 0}
    # NO SCOPE, NO PAIR. There is deliberately no fallback that compares blobs
    # when the scope is unknown: that fallback IS the phantom mass DROP, measured
    # on a real engine as `dropped 1` for a table the other catalog never held.
    rows = []
    if scope:
        sql = EXPLICIT_SQL if explicit else IMPLICIT_SQL
        params = {"cluster_id": cluster_id, "read_scope": scope}
        if explicit:
            params["snapshot_a"] = snapshot_a
            params["snapshot_b"] = snapshot_b
        rows = cache.execute(sql, params).rows

    for row in rows:
        before_blob = row.get("tables_before")
        schema_name = row.get("schema_name")
        if not explicit and not before_blob:
            # Latest row with no rn=2 partner: this schema has a baseline and
            # nothing to compare it against. Emitting a diff here would report
            # every existing table as newly added.
            baseline_only.append(schema_name)
            continue
        cmp_ = compare(schema_name, before_blob, row.get("tables_after"),
                       read_scope=scope, observation=observation)
        for k in totals:
            totals[k] += len(cmp_.diff[k])
        diffs.append({
            "schema_name": schema_name,
            # WHEN the change was recorded, per schema. Store-on-change means the
            # newest pair is the newest DDL EVENT, not a recent one: a diff dated
            # months ago is normal and the agent has to be able to see that.
            "snapshot_time": row.get("snapshot_after"),
            "previous_snapshot_time": row.get("snapshot_before"),
            # The two facts that qualify the diff, carried WITH it. A diff that
            # travels without them is what five passes handed the next consumer.
            "read_scope": cmp_.read_scope,
            "confirmation": cmp_.confirmation,
            "last_confirmed": cmp_.last_confirmed,
            **cmp_.diff,
        })

    coverage = _coverage(cache, cluster_id)
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
    if observation.get("status") == UNSUPPORTED_ENGINE:
        # THE REFUSAL, and it is tested FIRST because it is the reason the cluster is
        # empty. `not_collected` here would promise a baseline on the next ETL cycle
        # that is never coming, which is an empty success dressed as a young cluster.
        out["status"] = "not_supported"
        out["note"] = UNSUPPORTED_DIALECT_NOTE
    elif coverage["snapshots_stored"] == 0:
        out["status"] = "not_collected"
        out["note"] = _NOT_COLLECTED
    elif not scope:
        # History exists and none of it is comparable to anything. Distinct from
        # "no history" and from "no changes", and the previous pass had no status
        # for it at all: a pre-v27 cluster took the baseline-only branch and was
        # told a baseline exists, which is not what it was told about.
        out["status"] = "not_comparable"
        out["note"] = _NOT_COMPARABLE
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
