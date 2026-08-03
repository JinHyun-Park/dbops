"""ddl_estimator — calibrated lock + time footprint for a DDL statement.

Single source of truth shared by the Simulation MCP tool
(``simulate_ddl_impact``) and mirrored byte-for-byte into ``api/simulation/``
for the REST surface (the Lambda code asset is sandboxed per-function and
cannot import from ``mcp-servers``).

WHY this exists
---------------
Two problems it fixes together:

1. **Hardcoded throughput.** The runtime of a full-scan DDL (CREATE INDEX,
   table rewrite) was pinned to a flat ``40 MB/s`` regardless of the cluster's
   instance size or storage type — the same over-fitting we removed from the
   upgrade-time estimate. Sustained scan/rewrite throughput scales with the
   instance's IO bandwidth (≈ instance size) and is higher on I/O-Optimized
   storage, so we derive throughput from the cluster's REAL ``instance_class``
   (+ I/O-Optimized flag) instead of a constant.

2. **REST/MCP drift.** The REST mirror timed DDL as ``row_count/100k*5`` while
   the MCP tool used a size/operation model — so the dashboard and the agent
   disagreed. Both now call this module, so they cannot drift.

The estimate is OPERATION-CLASS driven (metadata-only ops are size-independent;
full-scan ops scale with size ÷ throughput), returned as a point estimate plus
a **range**, a **confidence**, and the **factors used** — directional guidance,
not a guarantee. Exact wall-clock still depends on concurrent load and cache
warmth, which we surface in the note.

Pure module: callers resolve the real signals (table size from ``table_stats``,
``instance_class`` from ``cluster_meta``) and pass them in.
"""

import re

# Full-scan throughput (MB/s) for a db.*.large-class instance on Standard
# storage. Larger instances get more IO bandwidth; I/O-Optimized adds headroom.
# 40 MB/s at the `large` tier preserves the previous default for that class.
_BASE_THROUGHPUT_MB_S = 40.0

# Relative IO-bandwidth tier by instance size token. Roughly tracks how Aurora
# instance throughput scales with size; deliberately coarse (the range + the
# "load/cache dependent" note carry the real uncertainty).
_SIZE_TIER = {
    "medium": 0.6,
    "large": 1.0,
    "xlarge": 1.6,
    "2xlarge": 2.6,
    "4xlarge": 4.5,
    "8xlarge": 8.0,
    "12xlarge": 11.0,
    "16xlarge": 14.0,
    "24xlarge": 20.0,
    "32xlarge": 24.0,
}
# Serverless v2 has no fixed class. The tier is derived from the cluster's MAX ACU
# (`cluster_meta.serverlessv2_max_acu`, written by meta_collector since schema_v9):
# 1 ACU is 2 GiB of memory, so max ACU maps onto the memory-equivalent instance size
# and reuses _SIZE_TIER above rather than inventing a second curve.
#
# Max, not min: a full-table DDL scan is a sustained load and Aurora scales the
# writer toward the ceiling within seconds, so the ceiling is what the scan gets.
#
# _SERVERLESS_TIER remains the fallback for a cluster whose ACU range has not been
# collected yet. It is ~xlarge, i.e. ~16 ACU: on a 2-4 ACU dev cluster that
# overstated throughput by 3-4x and understated the DDL window by the same factor.
_SERVERLESS_TIER = 2.0
# (max ACU ceiling, _SIZE_TIER key). Memory-equivalent r6g sizes: large is 16 GiB
# = 8 ACU, and each step doubles. Bucketed rather than interpolated because
# _SIZE_TIER itself is a coarse curve.
_ACU_TIERS = (
    (4, "medium"),
    (8, "large"),
    (16, "xlarge"),
    (32, "2xlarge"),
    (64, "4xlarge"),
    (128, "8xlarge"),
    (192, "12xlarge"),
    (256, "16xlarge"),
)
# I/O-Optimized storage sustains higher scan/write throughput.
_IO_OPT_MULTIPLIER = 1.4

# Metadata-only ops are near-instant; small floor for catalog lock + plan
# invalidation.
_METADATA_ONLY_SECONDS = 3
# CONCURRENTLY index builds avoid the write lock but do ~2 passes of the table.
_CONCURRENT_SLOWDOWN = 2.0

# Table-name resolver shared by both surfaces (kills a second drift: the MCP and
# REST tools used different regexes). Covers the common Aurora PG/MySQL DDL the
# agent simulates. CREATE INDEX names the table after ON (after the optional
# CONCURRENTLY + index name).
_TABLE_RX = re.compile(
    r"\b(?:ALTER\s+TABLE"
    r"|CREATE\s+(?:UNIQUE\s+|FULLTEXT\s+|SPATIAL\s+)?INDEX(?:\s+CONCURRENTLY)?\s+\S+\s+ON|"
    r"DROP\s+TABLE|TRUNCATE(?:\s+TABLE)?|REINDEX\s+(?:TABLE|INDEX)|VACUUM(?:\s+FULL)?|CLUSTER)\s+"
    r"(?:IF\s+EXISTS\s+)?([A-Za-z_\"][\w.\"]*)",
    re.IGNORECASE,
)


# --- MySQL online-DDL clauses -------------------------------------------------
# InnoDB lets the statement DECLARE how it wants to run, and refuses rather than
# degrades: an unsupported request raises 1845/1846 ("Try ALGORITHM=COPY",
# "Try LOCK=SHARED"). So a declared clause is proof of how the statement will
# run if it runs at all, which is why it overrides the shape-based default here.
# Every entry in the table below was measured against a real mysqld, not read off
# a docs page:
#   ADD/CREATE secondary INDEX (incl. UNIQUE, both statement forms) LOCK=NONE  ACCEPT
#   ADD FULLTEXT INDEX                                             LOCK=NONE  REJECT
#   ADD SPATIAL INDEX                                              LOCK=NONE  REJECT
#   MODIFY/CHANGE COLUMN <type>            INPLACE and LOCK=NONE              REJECT
#   ADD COLUMN / DROP COLUMN / RENAME COLUMN                  ALGORITHM=INSTANT ACCEPT
#   DROP INDEX                             ALGORITHM=INSTANT REJECT, INPLACE   ACCEPT
_ALGORITHM_RX = re.compile(r"\bALGORITHM\s*=\s*(INSTANT|INPLACE|COPY)\b")
_LOCK_RX = re.compile(r"\bLOCK\s*=\s*(NONE|SHARED|EXCLUSIVE)\b")

# Index kinds InnoDB cannot build with concurrent DML, whatever else is declared:
# "Fulltext index creation requires a lock" / "Do not support online operation on
# table with GIS index". Treating all MySQL index builds as online would promise
# these two away.
_NO_CONCURRENT_DML_INDEX = ("FULLTEXT", "SPATIAL")

# MySQL's spelling of a column type change. `CHANGE COLUMN` also renames, but it
# still carries a type, so it rebuilds the same way.
_MYSQL_TYPE_CHANGE_RX = re.compile(r"\b(?:MODIFY|CHANGE)\s+(?:COLUMN\s+)?\S+\s+\S")


def _is_mysql(engine) -> bool:
    """True for Aurora MySQL and standalone RDS MySQL, false for everything else.

    Both run InnoDB and share these online-DDL rules; PostgreSQL does not, and
    its ``CONCURRENTLY`` has no MySQL equivalent.
    """
    return "mysql" in str(engine or "").strip().lower()


def _requested(rx: re.Pattern, ddl_upper: str):
    """The value of a declared ALGORITHM= / LOCK= clause, or None."""
    m = rx.search(ddl_upper)
    return m.group(1) if m else None


def _adds_special_index(ddl_upper: str) -> bool:
    """ADD FULLTEXT/SPATIAL INDEX, which does not contain the substring
    "ADD INDEX" and so missed the index branch entirely."""
    return any(f"ADD {kind}" in ddl_upper for kind in _NO_CONCURRENT_DML_INDEX)


# Every shape that BUILDS an index. This was two substring tests, "CREATE INDEX"
# and "ADD INDEX", which silently missed the qualified forms: neither
# "CREATE UNIQUE INDEX" nor "ADD UNIQUE INDEX" contains either substring, so both
# fell through to the `other` fallback and were reported as
# "exclusive (assumed, unrecognized DDL)" with 0 MB of extra disk. That was wrong
# on PostgreSQL too: `CREATE UNIQUE INDEX CONCURRENTLY` came back as a blocking
# operation needing a maintenance window. Pre-existing, found by driving the
# classifier over the shapes a DBA actually writes rather than reading it.
#
# `ADD KEY` is MySQL's synonym for `ADD INDEX`. `ADD PRIMARY KEY` is deliberately
# NOT here: it rebuilds the table rather than adding a secondary index, so it
# belongs to a different cost class (see BACKLOG.md).
_INDEX_BUILD_RX = re.compile(
    r"\b(?:CREATE\s+(?:UNIQUE\s+|FULLTEXT\s+|SPATIAL\s+)?INDEX"
    r"|ADD\s+(?:UNIQUE\s+|FULLTEXT\s+|SPATIAL\s+)?(?:INDEX|KEY))\b"
)


def _mysql_changes_column_type(ddl_upper: str) -> bool:
    return bool(_MYSQL_TYPE_CHANGE_RX.search(ddl_upper))


def _mysql_operation(ddl_upper: str):
    """Operation label for a statement whose ALGORITHM=INSTANT already settled
    the cost. Only the label is in question here, not the lock or the scan."""
    for needle, op in (
        ("ADD COLUMN", "add_column"),
        ("DROP COLUMN", "drop_column"),
        ("RENAME COLUMN", "rename_column"),
        ("ADD INDEX", "create_index"),
        ("DROP INDEX", "drop_index"),
    ):
        if needle in ddl_upper:
            return op
    return None


def _mysql_index_lock(ddl_upper: str, algorithm, lock):
    """(online, lock_text) for a MySQL index build.

    An explicitly declared LOCK wins, because InnoDB would refuse a request it
    cannot honour. Otherwise the default is INPLACE with concurrent DML, except
    for the two index kinds that require a lock.
    """
    needs_lock = any(kind in ddl_upper for kind in _NO_CONCURRENT_DML_INDEX)
    if lock == "NONE" and not needs_lock:
        return True, "none (LOCK=NONE, concurrent DML permitted)"
    if lock == "SHARED":
        return False, "reads allowed, writes blocked (LOCK=SHARED)"
    if lock == "EXCLUSIVE":
        return False, "exclusive (LOCK=EXCLUSIVE)"
    if needs_lock:
        kind = next(k for k in _NO_CONCURRENT_DML_INDEX if k in ddl_upper)
        return False, (
            f"blocking: InnoDB refuses LOCK=NONE for a {kind} index "
            "(writes blocked during build)"
        )
    if algorithm == "COPY":
        return False, "blocking (ALGORITHM=COPY rebuilds the table)"
    return True, "none (InnoDB default ALGORITHM=INPLACE permits concurrent DML)"


def resolve_table(ddl_sql: str):
    """Best-effort target table name (schema + quotes stripped), or None.

    Split on '.' FIRST, then strip quotes from the last segment — so a quoted
    qualified name like ``"public"."orders"`` resolves to ``orders`` rather than
    ``"orders`` (stripping quotes first would leave an inner quote on the split)."""
    m = _TABLE_RX.search(ddl_sql or "")
    if not m:
        return None
    return m.group(1).strip().split(".")[-1].strip('"')


def classify_ddl(ddl_upper: str, engine=None) -> dict:
    """Classify DDL into (operation, scans_table, online, lock) by statement
    shape. Falls back to a conservative blocking estimate for anything
    unrecognized.

    ``engine`` (cluster_meta.engine) decides the ONLINE semantics. Without it
    the only online signal is PostgreSQL's ``CONCURRENTLY``, which MySQL never
    writes, so every MySQL index build was reported as "writes blocked during
    build" and the recommendation told the DBA to take a maintenance window for
    a statement InnoDB runs with concurrent DML permitted. Measured against a
    real mysqld: ``ALTER TABLE t ADD INDEX ix(a), LOCK=NONE`` is ACCEPTED.
    """
    has_concurrently = "CONCURRENTLY" in ddl_upper
    mysql = _is_mysql(engine)
    algorithm = _requested(_ALGORITHM_RX, ddl_upper)
    lock = _requested(_LOCK_RX, ddl_upper)

    # A declared ALGORITHM=INSTANT is metadata-only whatever the operation is,
    # and it cannot silently degrade: MySQL REFUSES an algorithm it cannot honour
    # (errors 1845 / 1846, both measured) instead of falling back to a rebuild.
    # So the declaration is evidence, not a hope.
    if mysql and algorithm == "INSTANT":
        return {
            "operation": _mysql_operation(ddl_upper) or "other",
            "scans_table": False,
            "online": True,
            "lock": "none (ALGORITHM=INSTANT, metadata only)",
        }

    # ADD PRIMARY KEY is its own cost class, and it does NOT match
    # _INDEX_BUILD_RX (that regex requires INDEX or KEY immediately after ADD, so
    # "ADD PRIMARY KEY" falls through it by construction). It used to land on the
    # `other` fallback, which reports scans_table=True but 0 MB of extra disk, for
    # an operation that rebuilds the whole table: on InnoDB the clustered index IS
    # the table, so adding a primary key rewrites it and needs room for a second
    # copy, exactly like the `rewrite` class.
    #
    # It is the one shape that is ONLINE AND EXPENSIVE at the same time. Measured
    # on a real mysqld: `ALTER TABLE p ADD PRIMARY KEY(id), LOCK=NONE` is ACCEPTED,
    # so InnoDB permits concurrent DML throughout, while still rebuilding. Neither
    # existing class expresses that, which is why it gets its own.
    if "ADD PRIMARY KEY" in ddl_upper:
        if mysql:
            if lock in ("SHARED", "EXCLUSIVE") or algorithm == "COPY":
                online, lock_text = False, (
                    f"blocking as declared (LOCK={lock})" if lock
                    else "blocking (ALGORITHM=COPY)")
            else:
                online, lock_text = True, (
                    "none (InnoDB rebuilds the clustered index but permits "
                    "concurrent DML)")
        else:
            # PostgreSQL has no CONCURRENTLY form of ADD PRIMARY KEY, so the bare
            # statement takes ACCESS EXCLUSIVE. No new claim is made here: this is
            # the same `online = has_concurrently` rule every other PG branch uses.
            online = has_concurrently
            lock_text = ("none (CONCURRENTLY)" if has_concurrently
                         else "exclusive (table rewrite + unique index build)")
        return {
            "operation": "add_primary_key",
            "scans_table": True,
            "online": online,
            "lock": lock_text,
        }

    if _INDEX_BUILD_RX.search(ddl_upper) or _adds_special_index(ddl_upper):
        if mysql:
            online, lock_text = _mysql_index_lock(ddl_upper, algorithm, lock)
        else:
            online = has_concurrently
            lock_text = (
                "none (CONCURRENTLY)"
                if has_concurrently
                else "blocking (writes blocked during build)"
            )
        return {
            "operation": "create_index",
            "scans_table": True,
            "online": online,
            "lock": lock_text,
        }
    if "REINDEX" in ddl_upper:
        return {
            "operation": "reindex",
            "scans_table": True,
            "online": has_concurrently,
            "lock": "none (CONCURRENTLY)" if has_concurrently else "blocking (index rebuild)",
        }
    if "VACUUM FULL" in ddl_upper or "CLUSTER" in ddl_upper:
        return {
            "operation": "rewrite",
            "scans_table": True,
            "online": False,
            "lock": "exclusive (full table rewrite + ~table-size extra disk)",
        }
    if ("ALTER COLUMN" in ddl_upper and "TYPE" in ddl_upper) or (
        mysql and _mysql_changes_column_type(ddl_upper)
    ):
        # MySQL writes this as MODIFY COLUMN / CHANGE COLUMN, which matched no
        # branch and fell through to "other" ("exclusive (assumed)"). It is the
        # same operation as PostgreSQL's ALTER COLUMN ... TYPE and needs the same
        # 1.0x extra-disk accounting, which only this branch reports.
        #
        # Measured: a column type change is ALWAYS a table rebuild on InnoDB,
        # even a varchar WIDENING. mysqld rejects both ALGORITHM=INPLACE and
        # LOCK=NONE with "Cannot change column type INPLACE. Try LOCK=SHARED",
        # so LOCK=SHARED (reads allowed, writes blocked) is the best case and
        # honouring a declared LOCK=NONE here would be wrong.
        return {
            "operation": "alter_column_type",
            "scans_table": True,
            "online": False,
            "lock": (
                "reads allowed, writes blocked (LOCK=SHARED is the best InnoDB "
                "offers for a type change)"
                if mysql
                else "exclusive (table rewrite)"
            ),
        }
    if "ADD COLUMN" in ddl_upper:
        # PG11+/MySQL8: ADD COLUMN with NO default is metadata-only (instant).
        # A DEFAULT (possibly volatile) or GENERATED can rewrite the table and on
        # PostgreSQL we can't prove constness from text, so be CONSERVATIVE.
        #
        # On InnoDB we CAN, because it answers: ADD COLUMN with a DEFAULT accepts
        # ALGORITHM=INSTANT (measured, including NOT NULL and CURRENT_TIMESTAMP
        # defaults), and so does a GENERATED ... VIRTUAL column. Only
        # GENERATED ... STORED is refused (1845), because it materializes a value
        # per row. Being conservative there told the DBA to take a maintenance
        # window for an instant catalog change.
        if mysql:
            stored_generated = "GENERATED" in ddl_upper and "STORED" in ddl_upper
            if stored_generated:
                return {
                    "operation": "add_column",
                    "scans_table": True,
                    "online": False,
                    "lock": (
                        "reads allowed, writes blocked: a STORED generated column "
                        "is materialized per row (InnoDB refuses LOCK=NONE)"
                    ),
                }
            return {
                "operation": "add_column",
                "scans_table": False,
                "online": True,
                "lock": "none (ALGORITHM=INSTANT, metadata only)",
            }
        if "DEFAULT" in ddl_upper or "GENERATED" in ddl_upper:
            return {
                "operation": "add_column",
                "scans_table": True,
                "online": False,
                "lock": "potentially blocking: DEFAULT/GENERATED may rewrite the table",
            }
        return {
            "operation": "add_column",
            "scans_table": False,
            "online": True,
            "lock": "brief (metadata-only, no default)",
        }
    if "DROP COLUMN" in ddl_upper:
        return {
            "operation": "drop_column",
            "scans_table": False,
            "online": False,
            "lock": "exclusive but metadata-only (fast)",
        }
    if "DROP INDEX" in ddl_upper:
        return {"operation": "drop_index", "scans_table": False, "online": False, "lock": "brief exclusive"}
    if "DROP TABLE" in ddl_upper or "TRUNCATE" in ddl_upper:
        return {
            "operation": "drop_or_truncate",
            "scans_table": False,
            "online": False,
            "lock": "exclusive but metadata-only (fast)",
        }
    return {"operation": "other", "scans_table": True, "online": False, "lock": "exclusive (assumed — unrecognized DDL)"}


def _acu_tier(max_acu):
    """(tier, size_token) for a Serverless v2 max-ACU value, or (None, "").

    Above the largest bucket the biggest tier is used rather than extrapolating:
    beyond ~256 ACU the bottleneck is no longer compute.
    """
    try:
        acu = float(max_acu)
    except (TypeError, ValueError):
        return None, ""
    if acu <= 0:
        return None, ""
    for ceiling, token in _ACU_TIERS:
        if acu <= ceiling:
            return _SIZE_TIER[token], token
    token = _ACU_TIERS[-1][1]
    return _SIZE_TIER[token], token


def throughput_mb_s(instance_class, io_optimized: bool, serverless_max_acu=None):
    """Derive full-scan throughput (MB/s) from the cluster's instance class +
    storage edition. Returns (mb_s, factors, tier_known).

    For Serverless v2, `serverless_max_acu` (cluster_meta.serverlessv2_max_acu)
    replaces the coarse mid-tier assumption. Omitted or unparseable, the assumption
    still applies, so an uncollected cluster degrades instead of failing."""
    factors: list[str] = []
    ic = (instance_class or "").lower()
    if "serverless" in ic:
        acu_tier, acu_token = _acu_tier(serverless_max_acu)
        if acu_tier is not None:
            tier = acu_tier
            tier_known = True
            factors.append(
                f"Serverless v2 최대 {float(serverless_max_acu):g} ACU "
                f"(메모리 환산 {acu_token} 상당, 크기 tier x{tier:g})"
            )
        else:
            tier = _SERVERLESS_TIER
            tier_known = True
            factors.append(
                "Serverless v2 — ACU 범위가 아직 수집되지 않아 중간 처리량을 가정했습니다"
                "(실제 처리량은 ACU에 비례)"
            )
    elif ic:
        token = ic.rsplit(".", 1)[-1]
        tier = _SIZE_TIER.get(token)
        tier_known = tier is not None
        if tier_known:
            factors.append(f"인스턴스 {instance_class} (크기 tier ×{tier:g})")
        else:
            tier = 1.0
            factors.append("인스턴스 클래스 미상 토큰 — large(×1.0) 기준 가정")
    else:
        tier = 1.0
        tier_known = False
        factors.append("인스턴스 클래스 미상 — large(×1.0) 기준 가정")

    mb_s = _BASE_THROUGHPUT_MB_S * tier
    if io_optimized:
        mb_s *= _IO_OPT_MULTIPLIER
        factors.append(f"I/O-Optimized 스토리지 ×{_IO_OPT_MULTIPLIER:g}")
    return round(mb_s, 1), factors, tier_known


# Operations that hold a SECOND COPY of the table until commit, so the extra-disk
# figure is ~1.0x the table. One tuple rather than a repeated literal, because the
# disk accounting and the rewrite recommendation have to name the same set: they
# used to be two separate literals and `add_primary_key` would have had to be added
# to both, which is how it ended up in neither.
_REBUILD_OPERATIONS = ("rewrite", "alter_column_type", "add_primary_key")


def _disk_needed_mb(operation: str, size_mb: float) -> float:
    if operation == "create_index":
        # New index covers a subset of columns; ~0.5× table size upper bound.
        return round(size_mb * 0.5, 1)
    if operation in _REBUILD_OPERATIONS:
        # A rewrite holds a second copy of the table until commit.
        return round(size_mb * 1.0, 1)
    return 0.0


def estimate_ddl(
    *,
    ddl_sql: str,
    table,
    row_count: int,
    size_mb: float,
    instance_class=None,
    io_optimized: bool = False,
    engine=None,
    serverless_max_acu=None,
) -> dict:
    """Full DDL impact estimate. Returns everything except ``cluster_id`` (the
    caller adds it). Time scales with table size ÷ instance-derived throughput
    for full-scan ops; metadata-only ops are size-independent.

    ``engine`` is cluster_meta.engine. It decides the online-DDL semantics and
    which rewrite tooling the recommendation may name; omitted, the model behaves
    exactly as it did before the argument existed (PostgreSQL rules).
    """
    ddl_upper = (ddl_sql or "").strip().upper()
    cls = classify_ddl(ddl_upper, engine)
    try:
        size_mb = float(size_mb or 0)
    except (TypeError, ValueError):
        size_mb = 0.0

    mb_s, tput_factors, tier_known = throughput_mb_s(
        instance_class, io_optimized, serverless_max_acu)
    basis: list[str] = [
        f"연산 분류: {cls['operation']} ({'테이블 풀스캔' if cls['scans_table'] else '메타데이터 전용'})"
    ]

    if cls["scans_table"]:
        est = max(5.0, size_mb / mb_s) if size_mb else 5.0
        if "CONCURRENTLY" in ddl_upper:
            est *= _CONCURRENT_SLOWDOWN
            basis.append(f"CONCURRENTLY: 락은 회피하나 ~{_CONCURRENT_SLOWDOWN:g}× 소요")
        basis.append(f"테이블 {size_mb:.0f}MB ÷ 추정 처리량 {mb_s:.0f}MB/s")
        basis.extend(tput_factors)
        size_known = size_mb > 0
        if not size_known:
            basis.append("테이블 크기 미상(table_stats 미수집) — 최소값 적용")
        confidence = "medium" if (tier_known and size_known) else "low"
        low, high = est * 0.5, est * 2.5
    else:
        est = float(_METADATA_ONLY_SECONDS)
        basis.append("메타데이터 전용 — 테이블 크기·인스턴스와 무관, 거의 즉시 완료")
        confidence = "high"
        low, high = est, float(_METADATA_ONLY_SECONDS * 3)

    disk_needed_mb = _disk_needed_mb(cls["operation"], size_mb)

    rebuilds = cls["operation"] in _REBUILD_OPERATIONS
    if cls["online"] and rebuilds:
        # ONLINE AND EXPENSIVE. InnoDB's ADD PRIMARY KEY permits concurrent DML
        # while rebuilding the whole table, so "서비스 영향 최소" would understate a
        # long operation that also needs room for a second copy. Neither of the
        # two branches below said that, which is the gap this class exists for.
        recommendation = (
            "쓰기를 막지 않지만 테이블 전체를 재작성합니다. 소요 시간이 길고 테이블 "
            "크기만큼 추가 디스크가 필요하므로, 여유 공간과 복제 지연을 함께 확인하세요"
        )
    elif cls["online"]:
        recommendation = "온라인 DDL 가능, 서비스 영향 최소"
    elif rebuilds:
        # Name the tooling that exists for THIS engine. pg_repack is a
        # PostgreSQL extension, and MySQL reaches this branch now that a
        # MODIFY COLUMN type change is classified instead of falling to "other".
        tooling = (
            "gh-ost/pt-online-schema-change"
            if _is_mysql(engine)
            else "pg_repack/BG 마이그레이션"
        )
        recommendation = f"테이블 전체 재작성/배타 락, 점검 윈도우 + {tooling} 검토"
    else:
        recommendation = "쓰기 차단/배타적 락, 점검 윈도우에서 수행 권장"

    if cls["scans_table"] or cls["operation"] == "add_column":
        # The ADD COLUMN caveat is engine-specific: on InnoDB a DEFAULT is stored
        # in the catalog (INSTANT), so repeating the PostgreSQL warning there
        # would contradict what this tool now reports for the same statement.
        add_column_note = (
            "InnoDB에서 ADD COLUMN은 DEFAULT가 있어도 ALGORITHM=INSTANT로 처리되며, "
            "GENERATED ... STORED 만 테이블 재작성을 유발합니다. "
            if _is_mysql(engine)
            else "ADD COLUMN은 상수/무default일 때만 메타데이터 변경이며 volatile default는 재작성을 유발합니다. "
        )
        note = (
            f"런타임은 추정치입니다 (테이블 {size_mb:.0f}MB ÷ {mb_s:.0f}MB/s, 인스턴스 클래스 기반). "
            + add_column_note
            + "실제 시간은 동시 부하·캐시 상태·I/O 경합에 따라 달라집니다."
        )
    else:
        note = "메타데이터 전용 작업으로 테이블 크기와 무관하게 거의 즉시 완료됩니다."

    return {
        "ddl": ddl_sql,
        "table": table or "unknown",
        "operation": cls["operation"],
        "table_info": {"rows": int(row_count or 0), "size_mb": round(size_mb, 1)},
        "estimated_seconds": round(est),
        "estimated_range_seconds": [round(low), round(high)],
        "online_ddl_possible": cls["online"],
        "lock_type": cls["lock"],
        "disk_space_needed_mb": disk_needed_mb,
        "throughput_mb_s": mb_s,
        "confidence": confidence,
        "basis": basis,
        "recommendation": recommendation,
        "note": note,
    }
