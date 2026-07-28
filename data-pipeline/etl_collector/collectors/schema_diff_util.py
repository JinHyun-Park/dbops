"""Canonical schema_snapshots SELECTION CONTRACT + parse + diff.

Shared by all FOUR readers and the PRODUCER, because they all answer the same
question off the same rows and they used to answer it four different ways:

  mcp operations get_schema_diff      recomputes a diff from two blobs
  mcp operations get_schema_history   replays the stored diffs
  mcp incident   diagnose_root_cause  replays the stored diffs
  api/dashboard  _schema_changes      recomputes a diff from two blobs
  the collectors                      compare this read against the stored blob

WHY THIS FILE OWNS THE **SELECTION** AND NOT ONLY THE DIFF
----------------------------------------------------------
compute_diff was already shared. What was NOT shared was the SQL that decides
WHICH TWO BLOBS ARE COMPARABLE, and that duplication is the whole defect. Six
passes were made over one bug; every pass fixed the subset of consumers its file
ownership happened to include, and the defect survived in the layer it did not
touch:

  1  the dashboard `dropped` branch was unreachable, so a real DROP was never
     reported at all.
  2  fixed, and the stale-cluster case became a self-comparison reported as ok.
  3  fixed, and the vanished-schema guard was made unreachable in the same commit.
  4  fixed with a corroboration predicate that `public` satisfies in every
     PostgreSQL database, so a read of the WRONG DATABASE corroborated itself and
     the phantom mass DROP moved from the edge case to the common case.
  5  deleted the absence inference and added `read_scope` + `last_seen_at`
     (schema_v27) so comparability could be decided at all. It landed in the
     collector and the two MCP readers. The two BLOB-DIFF readers still selected
     their pairs with no notion of read_scope, so the phantom DROP was still
     live; MEASURED on PostgreSQL 14.18, pre-v27 history plus one read of the
     wrong database: get_schema_diff `dropped 1` (public [audit]) and the
     dashboard panel `[('created','public','app_settings'),
     ('dropped','public','audit')]`, both status ok, and the R3 freeze then
     stopped collection permanently so it survived the operator fixing the
     config.

So the rule that stops a seventh pass is mechanical, not editorial:

  NO CONSUMER MAY WRITE `FROM schema_snapshots`.

Every read is built from SCOPED_ROWS or ALL_ROWS below, and
tests/unit/data_pipeline/test_schema_snapshot_parity.py fails if that text
appears in any consumer file. That single test is what the five previous passes
would each have failed.

And no consumer may call compute_diff: `compare()` is the only licensed way to
obtain a diff, and it CANNOT be called without the read_scope and the
per-schema confirmation state, so a consumer that wants a diff is forced to
select them too. Same test enforces it.

COMPARABILITY
-------------
Two snapshots are comparable ONLY under the same `read_scope` (the catalog the
read reached, reported by the read: see data-pipeline/schema_migrator/sql/
schema_v27.sql). NULL is the scope of every pre-v27 row and it is comparable to
NOTHING, including another NULL: `read_scope = :read_scope` never matches NULL in
SQL, which is the point. Treating NULL as a wildcard is what let a wrong-database
baseline sit next to real history and be diffed against it.

CONFIRMATION
------------
Under store-on-change, `snapshot_time` is when a schema last CHANGED (months ago
for a stable schema), so it cannot distinguish "unchanged for months" from "not
seen for months". That ambiguity IS what four passes resolved to "dropped".
`last_seen_at` answers it, and it is read PER SCHEMA against an ABSOLUTE bar,
never against the cluster-wide MAX: a cycle that confirms nothing (a scope
change, a collector error) leaves every schema equal to that max, which reported
zero unconfirmed schemas while the collector's own return value said two. MEASURED
before this change, a frozen cycle over a 2-schema cluster: collector
`{"not_seen": 2, "not_seen_schemas": ["alpha", "public"]}` and both readers
`{"status": "fresh", "unconfirmed_schemas": []}`.

THE ACCEPTED COST, kept visible: a genuine DROP SCHEMA / DROP DATABASE is NOT
reported as a drop anywhere. Absence cannot be told apart from a read that could
not reach the schema, so it surfaces in every consumer as "last confirmed at T,
not seen since" and never as "no changes".

DIALECT: POSTGRESQL ONLY, AND THAT IS A REFUSAL RATHER THAN A GAP
-----------------------------------------------------------------
Every claim on this surface rests on "absent from the catalog read means absent
from the database". That holds on PostgreSQL and NOT on MySQL, where
information_schema is privilege-filtered in all three diff buckets and the scope
key (CURRENT_USER()) does not move when visibility does: measured numbers are in
snapshot_dialect_supported(). So the gate is POSITIVE: only a PostgreSQL catalog
read is collected, and every other engine (MySQL, SQL Server, DocumentDB,
DynamoDB, ElastiCache) is refused by the same rule for reasons listed per engine on
that predicate. Every reader reports `unsupported_engine` instead of an empty
success, and the shared sentence states the PostgreSQL rule rather than MySQL's
reason, because four of the five refused families do not have MySQL's reason. The
dialect is resolved PER CLUSTER from cluster_meta.engine, never from a capability
key, because Aurora MySQL and Aurora PostgreSQL are the same engine FAMILY.

COPIES. api/ cannot import mcp_servers and the collector assets cannot either, so
this file is FOUR verbatim copies (edit them together; the parity test asserts
byte-identity AND identical results):
  mcp-servers/mcp_servers/shared/schema_diff_util.py            <- canonical
  api/dashboard/schema_diff_util.py
  data-pipeline/etl_collector/collectors/schema_diff_util.py
  data-pipeline/rds_direct_collector/schema_diff_util.py

Diff heuristics:
  - Same name, different column-name set -> modified
  - Name in before, missing in after     -> dropped
  - Name in after, missing in before     -> added
  - Dropped + added pair with IDENTICAL column signatures -> rename candidate
    (surfaced separately so the agent can confirm instead of claiming a DROP)
Only column NAMES are compared. Types are accepted on input and ignored.
"""

import json
from dataclasses import dataclass
from typing import Any, Optional

# ---------------------------------------------------------------------------
# WHICH ROWS. The only two row sources in the repo, and the difference between
# them is a decision, not a convenience.
# ---------------------------------------------------------------------------

# COMPARISON. Anything that puts two blobs side by side uses this. `read_scope =
# :read_scope` excludes NULL by SQL's own rules, which is exactly the intent: a
# row whose scope is unknown is not comparable to anything, so the first
# scope-known read re-baselines it once instead of being diffed against it.
SCOPED_ROWS = (
    "FROM schema_snapshots "
    "WHERE cluster_id = :cluster_id AND read_scope = :read_scope"
)

# REPLAY. A stored diff_from_previous_json was computed by the producer against a
# SAME-SCOPE predecessor by construction, so replaying it is complete and safe
# without the scope predicate. Filtering these by the CURRENT scope would delete
# real DDL history from the record every time a cluster is re-scoped, which is
# the opposite failure from the one this file exists to prevent.
ALL_ROWS = "FROM schema_snapshots WHERE cluster_id = :cluster_id"

# THE PRODUCER'S SIDE OF THE SAME DECISION. The heartbeat has to find the latest
# row of ONE schema under THIS scope, which is the same comparability rule in a
# scalar subquery, so it lives here too. Without this the collector would carry the
# fourth `FROM schema_snapshots` in the repo and the mechanical test could not be
# an equality.
LATEST_SCOPED_TIME_SUBQUERY = (
    "(SELECT MAX(x.snapshot_time) FROM schema_snapshots x "
    " WHERE x.cluster_id = :cluster_id AND x.schema_name = :schema_name "
    "   AND x.read_scope = :read_scope)"
)

# WHICH DIALECT THIS CLUSTER IS, resolved PER CLUSTER and not per engine family.
# Aurora MySQL and Aurora PostgreSQL are the SAME family (`relational`), so a
# capability key cannot express this decision; cluster_meta.engine can, and reading
# it is the prior art of operations/tools/prewarm_reader.py:_is_postgres.
CLUSTER_ENGINE_SQL = "SELECT engine FROM cluster_meta WHERE cluster_id = :cluster_id"


def snapshot_dialect_supported(engine: Any) -> bool:
    """POSITIVE, FAIL-CLOSED gate: is this engine's catalog free of privilege
    filtering, so that an absent table means an absent table?

    PostgreSQL only, and this is a REFUSAL rather than another predicate. MEASURED
    on MySQL 9.3.0 against the shipped catalog read, as the collecting identity:

      full grant on appdb        {"users": ["email","id"], "orders": ["id","total"]}
      DROP TABLE dropdb.users    dropdb table_count 0, tables_json {}
      table-level REVOKE         appdb {"users": ["email","id"]}   <- `orders` gone
      column-level GRANT (id)    appdb {"users": ["id"]}           <- `email` gone
      whole database REVOKEd     appdb absent from the read entirely
      read_scope, every time     collector@localhost

    So on MySQL a REVOKE is byte-identical to a DROP in all three diff buckets
    (dropped, modified, and the schema vanishing), and CURRENT_USER() does not move
    when it happens: tightening the collector user to least privilege is
    indistinguishable from a DBA dropping every table. There is no scope key that
    changes with visibility (the grant set does, but it is not the visibility: role
    grants, proxy users and column-level grants all move visibility without moving
    any key this read can select), so the product refuses instead of guessing.

    PostgreSQL is NOT exposed: measured on 14.18, an unprivileged role gets the
    IDENTICAL PG_SCHEMA_SQL result as the superuser (pg_namespace and pg_class are
    not privilege-filtered) while `SELECT FROM core.users` raises permission denied.

    THE OTHER FAMILIES THIS ALSO REFUSES, and their grounds are not MySQL's:
      sqlserver-*   no snapshot collector was ever written for it. Its
                    rds_direct branch does not call one, so nothing is lost by
                    refusing and nothing about its catalog is claimed here.
      documentdb    schema-less document model: there is no relational
                    schema/table/column catalog for a diff to be defined over.
      dynamodb      a table has no fixed column set beyond its key attributes.
      redis/valkey/memcached  key-value, no catalog at all.
    MySQL is the only MEASURED negative on this list of reasons; for the four above
    the premise was never established rather than disproved.
    UNSUPPORTED_DIALECT_NOTE therefore states the POSITIVE rule (snapshots are
    collected off the PostgreSQL catalog) rather than handing every refused engine
    MySQL's reason for its cluster.

    Fail-closed on an unknown engine is the CALLER's job, not this predicate's:
    `observed()` reports an engine it could not resolve as `unavailable`, which is
    "we could not decide", not "this engine is not supported".
    """
    return "postgres" in str(engine or "").lower()


# The cluster's ESTABLISHED scope: the scope of the newest row that HAS one. A
# NULL-scope row can never win it, so a cluster whose whole history predates
# schema_v27 has NO established scope and nothing about it is comparable until
# the collector runs once.
ESTABLISHED_SCOPE_SQL = (
    "SELECT read_scope " + ALL_ROWS + " AND read_scope IS NOT NULL "
    "ORDER BY snapshot_time DESC LIMIT 1"
)

# WAS THIS SCHEMA STILL THERE, per schema. `age_sec` is each schema's OWN age,
# never the cluster-wide MAX (see the module docstring). -1 stands in for a NULL
# last_seen_at (pre-v27, or a scope that has never confirmed this schema), which
# is "never confirmed" and must not compare as recent.
OBSERVED_SQL = (
    "SELECT schema_name, COALESCE(read_scope, '') AS read_scope, "
    "       last_seen_at::text AS last_seen, "
    "       CASE WHEN tables_json <> '{}'::jsonb THEN 'y' ELSE 'n' END AS holds_tables, "
    "       COALESCE(EXTRACT(EPOCH FROM (NOW() - last_seen_at))::bigint, -1) AS age_sec "
    "FROM ("
    "  SELECT DISTINCT ON (schema_name) schema_name, read_scope, last_seen_at, "
    "         tables_json, snapshot_time "
    "  " + ALL_ROWS +
    "  ORDER BY schema_name, snapshot_time DESC"
    ") latest ORDER BY schema_name"
)

# Coverage of the whole cluster, every scope: what EXISTS, as opposed to what is
# comparable. Reported beside a negative so "nothing to compare" and "nothing
# changed" cannot read the same way.
COVERAGE_SQL = (
    "SELECT COUNT(*) AS snapshots, COUNT(DISTINCT schema_name) AS schemas, "
    "       MIN(snapshot_time)::text AS first_seen, MAX(snapshot_time)::text AS last_seen "
    + ALL_ROWS
)

# Three ETL cycles at the default 5-minute interval, the same bar
# api/clusters/handler.py uses for etl_status and api/dashboard/handler.py for
# _FRESH_MAX_AGE_SEC. Inside it, "not confirmed yet" is not yet news.
CONFIRM_WITHIN_SEC = 15 * 60

# Per-schema confirmation. Four values, and the three that are not `confirmed`
# all mean the same thing to a DBA (we cannot say this schema is still there),
# while meaning three different things to whoever fixes the collector.
CONFIRMED = "confirmed"          # named by a scope-matching read inside the bar
NOT_SEEN = "not_seen"            # matching scope, but not confirmed inside the bar
UNKNOWN_SCOPE = "unknown_scope"  # its newest row was recorded under another scope
UNMIGRATED = "unmigrated"        # its newest row carries no scope at all (pre-v27)

# Cluster-level observation status. EVERY value is first-class: the panel state
# matrix crosses all of them and the parity test asserts none exists outside it.
# `stale` from the previous pass is gone on purpose: a cluster nothing has
# confirmed lately is a cluster where every schema is NOT_SEEN, and that says the
# same thing while NAMING the schemas, which `stale` could not.
OBSERVATION_STATUSES = ("fresh", "not_seen", "unmigrated", "no_snapshots",
                        "unavailable", "unsupported_engine")

# THE REFUSAL. This cluster's engine has a privilege-filtered catalog, so no read of
# it can tell a DROP from a REVOKE (see snapshot_dialect_supported for the numbers).
# It is a first-class observation status because every reader has to say so instead
# of reporting an empty success: `not_collected` on such a cluster would promise a
# baseline on the next ETL cycle that is never coming.
UNSUPPORTED_ENGINE = "unsupported_engine"

# A `dropped` list is only ever as good as the catalog the collector could read, and
# on MySQL that catalog is privilege-filtered in every bucket, so the product does
# not collect schema snapshots there at all. The caveat stays on the PostgreSQL path
# for the honest residue: what the collecting role could see at that moment.
DROPPED_CAVEAT = (
    "dropped 목록은 수집 계정이 그 시점에 볼 수 있었던 카탈로그를 기준으로 계산됩니다. "
    "PostgreSQL 카탈로그(pg_namespace/pg_class)는 권한으로 필터링되지 않으므로 이 목록은 "
    "실제 DDL을 반영합니다. MySQL 계열은 information_schema가 권한 필터링되어 REVOKE와 "
    "DROP을 구분할 수 없기 때문에 스키마 스냅샷 수집 대상이 아닙니다."
)

# What every reader says about a refused dialect. One sentence, shared, so the panel
# and the two MCP tools and the agent narrative cannot describe it three ways.
#
# IT STATES THE POSITIVE RULE, not one engine's reason. This note used to explain
# MySQL's privilege-filtered information_schema, and `snapshot_dialect_supported` is
# False for documentdb, dynamodb, redis/valkey/memcached and sqlserver-* as well, so
# a DocumentDB operator was handed MySQL's reason for their cluster. There is one
# rule and it is a rule about PostgreSQL, so that is what the sentence says; the
# per-engine grounds (including MySQL's measured numbers) live in
# snapshot_dialect_supported above, which is where a reader who wants them looks.
UNSUPPORTED_DIALECT_NOTE = (
    "스키마 스냅샷(테이블 생성·삭제 판정)은 PostgreSQL 카탈로그(pg_namespace/pg_class)를 "
    "읽는 cluster에서만 수집합니다. 이 카탈로그는 권한으로 필터링되지 않아 '읽기 결과에 "
    "없으면 실제로 없다'가 성립하기 때문입니다. 이 cluster의 엔진은 그 전제가 확인된 "
    "대상이 아니어서 스냅샷을 수집하지 않으며, 따라서 이 cluster에 대해서는 '변경 없음'도 "
    "'변경 있음'도 말할 수 없습니다. 엔진별 근거는 snapshot_dialect_supported()에 "
    "기록되어 있습니다."
)


def parse_tables(blob: Any) -> dict[str, list[str]]:
    """tables_json on a snapshot is `{table_name: [col1, col2, ...]}` in the
    canonical shape. A jsonb column comes back from the RDS Data API as a
    string, so handle both. `{col_name: type}` per table is also accepted;
    only the keys are kept."""
    if not blob:
        return {}
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except json.JSONDecodeError:
            return {}
    if not isinstance(blob, dict):
        return {}
    # Normalize column lists to sorted lists so unordered representations
    # (JSON_ARRAYAGG has no ORDER BY in MySQL) still compare cleanly.
    out: dict[str, list[str]] = {}
    for tname, cols in blob.items():
        if isinstance(cols, list):
            out[str(tname)] = sorted(str(c) for c in cols)
        elif isinstance(cols, dict):
            out[str(tname)] = sorted(str(k) for k in cols.keys())
    return out


def compute_diff(before: dict[str, list[str]], after: dict[str, list[str]]) -> dict:
    """Four-bucket diff: added, dropped, modified, rename_candidates.

    NOT FOR CONSUMERS. Call `compare()`, which cannot be called without the
    read_scope and the confirmation state that decide whether these two maps were
    comparable in the first place. `dropped` is inferred from ABSENCE, so a caller
    that feeds it two blobs from different catalogs reports a phantom mass DROP,
    which is the defect six passes chased. The parity test fails on a direct call
    from any consumer file.
    """
    before_names = set(before)
    after_names = set(after)

    added_names = after_names - before_names
    dropped_names = before_names - after_names
    common = before_names & after_names

    modified = []
    for name in sorted(common):
        if before[name] != after[name]:
            modified.append({
                "table": name,
                "added_columns": sorted(set(after[name]) - set(before[name])),
                "dropped_columns": sorted(set(before[name]) - set(after[name])),
            })

    rename_candidates = []
    consumed_drops: set[str] = set()
    consumed_adds: set[str] = set()
    for d in sorted(dropped_names):
        for a in sorted(added_names):
            if a in consumed_adds:
                continue
            if before[d] == after[a]:
                rename_candidates.append({"from": d, "to": a})
                consumed_drops.add(d)
                consumed_adds.add(a)
                break

    return {
        "added": sorted(added_names - consumed_adds),
        "dropped": sorted(dropped_names - consumed_drops),
        "modified": modified,
        "rename_candidates": rename_candidates,
    }


def diff_is_empty(diff: dict) -> bool:
    """True when a computed diff describes no change at all. The collector uses
    this to decide whether a snapshot is worth storing, so an unchanged schema
    never produces a row the readers would then have to filter out."""
    return not any(diff.get(k) for k in ("added", "dropped", "modified", "rename_candidates"))


@dataclass(frozen=True)
class SchemaComparison:
    """A diff that CANNOT be obtained without the two facts that qualify it.

    Every previous pass ended with a consumer holding a bare diff dict and no way
    to know whether the two blobs behind it described the same catalog, or whether
    the schema was even still there. So the diff is not returned on its own: it
    arrives welded to the scope it was computed under and to the confirmation
    state of the schema, and a consumer that renders `diff` while ignoring
    `confirmation` is reporting an absence of change it cannot support.
    """

    schema_name: str
    read_scope: str
    confirmation: str
    last_confirmed: Optional[str]
    diff: dict

    @property
    def is_empty(self) -> bool:
        return diff_is_empty(self.diff)

    @property
    def is_confirmed(self) -> bool:
        return self.confirmation == CONFIRMED


def observed(query, cluster_id: str) -> dict:
    """The confirmation state of every schema this cluster has history for.

    `query(sql, params) -> list[dict]`. api/ and mcp_servers pass their own
    callable; the collectors do not use this (they hold the read that produces
    the fact, not the one that interprets it).

    Returns, and every key is part of the contract:
      status               one of OBSERVATION_STATUSES
      read_scope           the cluster's established scope, or None. NOTHING is
                           comparable without it, so a consumer with None here
                           must not select a pair at all.
      last_confirmed       the newest per-schema confirmation, for the sentence
                           the operator reads
      schemas              {name: {confirmation, last_seen, holds_tables}}
      unconfirmed_schemas  the schemas still SERVING TABLES that this cluster
                           cannot currently confirm. A schema holding no tables is
                           not in here: nobody is being shown stale contents for
                           it, so it is not a blindness.
    """
    # THE DIALECT FIRST, before anything is counted. It has to come first because
    # the alternative is `no_snapshots` on a refused engine, and that status'
    # sentence promises a baseline on the next ETL cycle which is never coming: an
    # empty success. An engine we could not RESOLVE is `unavailable`, not
    # `unsupported_engine`: "we cannot decide" is not "this is not supported", and a
    # cluster registered minutes ago has no cluster_meta row yet.
    try:
        erows = query(CLUSTER_ENGINE_SQL, {"cluster_id": cluster_id})
        engine = (erows[0].get("engine") if erows else None) or ""
    except Exception as e:
        print(f"[schema] engine lookup unavailable: {type(e).__name__}: {e}")
        engine = ""
    if not engine:
        return {"status": "unavailable", "read_scope": None, "last_confirmed": None,
                "schemas": {}, "unconfirmed_schemas": []}
    if not snapshot_dialect_supported(engine):
        return {"status": UNSUPPORTED_ENGINE, "read_scope": None,
                "last_confirmed": None, "schemas": {}, "unconfirmed_schemas": []}
    try:
        rows = query(ESTABLISHED_SCOPE_SQL, {"cluster_id": cluster_id})
        scope = (rows[0].get("read_scope") if rows else None) or None
        rows = query(OBSERVED_SQL, {"cluster_id": cluster_id})
    except Exception as e:
        # A cache DB without schema_v27, no permission, cache down. Detail to
        # CloudWatch only: no exception text may reach a response payload.
        print(f"[schema] observation probe unavailable: {type(e).__name__}: {e}")
        return {"status": "unavailable", "read_scope": None, "last_confirmed": None,
                "schemas": {}, "unconfirmed_schemas": []}
    if not rows:
        return {"status": "no_snapshots", "read_scope": None, "last_confirmed": None,
                "schemas": {}, "unconfirmed_schemas": []}

    schemas: dict[str, dict] = {}
    unconfirmed: list[str] = []
    confirmed_times: list[str] = []
    for r in rows:
        name = r.get("schema_name")
        if not name:
            continue
        row_scope = r.get("read_scope") or ""
        holds = r.get("holds_tables") == "y"
        last_seen = r.get("last_seen")
        try:
            age = int(r.get("age_sec"))
        except (TypeError, ValueError):
            age = -1
        if not row_scope:
            state = UNMIGRATED
        elif scope is None or row_scope != scope:
            state = UNKNOWN_SCOPE
        elif age < 0 or age > CONFIRM_WITHIN_SEC:
            state = NOT_SEEN
        else:
            state = CONFIRMED
        # Every stamp made under the ESTABLISHED scope is a real confirmation of that
        # schema at that moment, whether or not it is still inside the bar. Only the
        # scopes that confirm nothing now (UNKNOWN_SCOPE, UNMIGRATED) are excluded.
        if last_seen and state in (CONFIRMED, NOT_SEEN):
            confirmed_times.append(last_seen)
        schemas[name] = {"confirmation": state, "last_seen": last_seen,
                         "holds_tables": holds}
        if state != CONFIRMED and holds:
            unconfirmed.append(name)

    if scope is None:
        status = "unmigrated"
    elif unconfirmed:
        status = "not_seen"
    else:
        status = "fresh"
    return {
        "status": status,
        "read_scope": scope,
        # The newest confirmation under the ESTABLISHED SCOPE, fresh or not. Not
        # MAX(last_seen_at) over every row, because a row under an abandoned scope
        # carries a stamp that confirms nothing now; and NOT restricted to the stamps
        # that are still inside the bar, because when a whole cluster has gone
        # unconfirmed that restriction leaves this None and the operator loses the one
        # number the sentence needs: WHEN it was last confirmed. Driven: a cluster
        # whose last cycle was 30 minutes ago reported last_confirmed None before this.
        "last_confirmed": max(confirmed_times) if confirmed_times else None,
        "schemas": schemas,
        "unconfirmed_schemas": sorted(unconfirmed),
    }


def observation_is_complete(obs: dict) -> bool:
    """True only when every table-holding schema was confirmed under the
    established scope. ONE condition, deliberately: every previous pass added
    another `and not <list>` to a compound predicate and the next state escaped
    through the condition nobody added. Anything that is not `fresh` means part of
    the question was not looked at, so a negative cannot cover the cluster."""
    return obs.get("status") == "fresh"


def compare(schema_name: str, before_blob: Any, after_blob: Any, *,
            read_scope: str, observation: dict) -> SchemaComparison:
    """THE ONLY licensed way to turn two stored blobs into a diff.

    Keyword-only `read_scope` and `observation` are the enforcement: a consumer
    cannot reach a diff without having selected the scope the pair was recorded
    under and the confirmation state of the schema, which is exactly what the two
    blob-diff readers were missing for five passes.

    Raises ValueError on a missing scope rather than comparing anyway. There is no
    fallback, because "compare them anyway when the scope is unknown" IS the
    phantom mass DROP: it is how a baseline written from the wrong database got
    diffed against real history recorded from the right one.
    """
    if not read_scope:
        raise ValueError(
            "compare() needs the read_scope the pair was recorded under. Two "
            "snapshots with no common scope are not comparable, and diffing them "
            "reports a phantom DROP for every table the other catalog did not "
            "hold. Select the pair with SCOPED_ROWS."
        )
    state = (observation.get("schemas") or {}).get(schema_name) or {}
    return SchemaComparison(
        schema_name=schema_name,
        read_scope=read_scope,
        confirmation=state.get("confirmation", UNKNOWN_SCOPE),
        last_confirmed=state.get("last_seen"),
        diff=compute_diff(parse_tables(before_blob), parse_tables(after_blob)),
    )


def not_seen_note(obs: dict) -> str:
    """Korean prose for whatever the observation cannot support, empty when the
    cluster was fully confirmed. Shared so all four consumers say the SAME thing
    about the same state: the panel had no sentence for this at all, which is how
    a genuine DROP SCHEMA reached the operator as "no changes detected".

    ADDITIVE, not a first-match ladder: a cluster-level problem and a named
    unconfirmed schema are different unknowns and can coexist, so reporting only
    the first drops the schema NAME the DBA needs.
    """
    parts = []
    status = obs.get("status")
    if status == UNSUPPORTED_ENGINE:
        # A REFUSAL, not a failure, and it is the whole sentence: there is no
        # per-schema detail to add because nothing was collected on purpose.
        return UNSUPPORTED_DIALECT_NOTE
    if status == "unavailable":
        # BOTH causes, because they lead to different operator actions: apply the
        # migration, or wait one collection cycle for cluster_meta to land.
        parts.append("스키마 관측에 필요한 정보를 조회할 수 없어(캐시 DB에 schema_v27 "
                     "미적용이거나 이 cluster의 cluster_meta 행이 아직 없음) 각 "
                     "스키마가 현재도 존재하는지는 확인하지 못했습니다.")
    elif status == "unmigrated":
        parts.append("저장된 스냅샷이 모두 schema_v27 이전 기록이라 어떤 카탈로그를 "
                     "읽은 것인지 알 수 없어 비교 대상으로 쓸 수 없습니다. 다음 수집 "
                     "주기에 각 스키마가 baseline으로 다시 기록됩니다.")
    unconfirmed = obs.get("unconfirmed_schemas") or []
    if unconfirmed:
        # PER SCHEMA, because they were last confirmed at DIFFERENT times and one
        # cluster-level timestamp misattributes the newest read's time to a schema
        # nobody has seen for ten days. Driven: a pre-v27 history plus one read of
        # another database reported "마지막 확인 시각은 <that read's time>" for two
        # schemas that read never touched.
        detail = ", ".join(
            f"{n}(마지막 확인 {(obs.get('schemas') or {}).get(n, {}).get('last_seen') or '기록 없음'})"
            for n in unconfirmed)
        parts.append(
            f"{detail} 스키마는 최근 카탈로그 읽기에서 확인되지 않았습니다. "
            "삭제되었을 수도 있고 읽기가 도달하지 못한 것일 수도 있어, 삭제로 "
            "단정하지 않고 '확인 불가'로 보고합니다. 이 스키마의 테이블 목록은 마지막 "
            "확인 시점의 상태입니다."
        )
    return " ".join(parts)
