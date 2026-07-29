"""upgrade_estimator — calibrated engine upgrade time/downtime model.

Calibrated on Aurora, and the REST ``/upgrade-impact`` + ``/upgrade-plan``
routes carry no engine-family gate, so a registered standalone RDS MySQL
instance reaches this model too. Version PARSING is therefore engine-aware
(see ``major_family``); the time model itself is not, so an rds_instance
estimate is directionally useful rather than calibrated for that form.

This is the single source of truth for "how long will this upgrade take and
how much downtime will it cost", shared by the Simulation MCP tools
(``estimate_upgrade_impact`` / ``generate_upgrade_plan``) and mirrored
byte-for-byte into ``api/simulation/`` for the REST surface (the Lambda code
asset is sandboxed per-function and cannot import from ``mcp-servers``).

WHY this replaces the old ``base + storage/100 * coeff`` heuristic
-----------------------------------------------------------------
The old model made wall-clock scale with raw storage. That is wrong for the
mechanics AWS actually documents:

* **MINOR upgrades just swap binaries** — no data-file rewrite — so the cost
  is essentially a writer reboot plus a small per-reader patch. It is largely
  independent of data size *and* object count.
  (AWS: minor upgrades "only replace binaries without changing data files".)

* **MAJOR upgrades run pg_upgrade (PostgreSQL) / precheck+upgrade (MySQL)**,
  and AWS guidance + field reports agree the dominant driver is the
  **NUMBER OF DATABASE OBJECTS** (tables / indexes / routines), NOT raw
  storage: a 1 TB / 1k-table database upgrades far faster than a
  100 GB / 100k-table one. Large, busy clusters have measured ~1 hour of
  in-place pg_upgrade downtime (AWS "Wiz" case study).

* **The method changes DOWNTIME, not just total wall-clock:**
    - ``in_place``  — the writer is offline for ~the whole upgrade compute
      window (minutes for a minor, up to ~1 h for a large major).
    - ``blue_green`` — the green environment is built and caught up in the
      background; the only downtime is the **switchover**, which RDS guardrails
      bound to well under a minute (timeout configurable 30 s–1 h, default 5 m).
    - ``clone``     — the source cluster is untouched; downtime is just the
      endpoint cutover.

* **Object count drives majors**, so we read the live table count from the
  ``table_stats`` cache when available; without it we fall back to a coarse
  default and drop confidence to ``"low"``.

Because real durations also depend on workload (MySQL undo/history-list
length, write rate during blue/green replication catch-up), the estimate is
returned as a **range** plus a **confidence** and the **factors used**, and the
response recommends the AWS-blessed way to get an exact number: clone the
cluster and time a trial upgrade.

Everything here is pure: callers resolve the real signals (storage, engine,
versions, reader count, table count) and pass them in, so this module has no
AWS/cache dependencies and is trivially unit-testable.
"""

# --- Minor upgrade: binary swap + reboot. Size/object-count independent. ----
_MINOR_WRITER_MIN = 6          # writer binary patch + reboot/failover
_MINOR_PER_READER_MIN = 2      # each reader patched after the writer

# --- Major upgrade: object-count driven (pg_upgrade / MySQL precheck). ------
_MAJOR_BASE_MIN = 12           # prechecks + pre/post snapshot + reboot
_MAJOR_PER_1K_OBJECTS_MIN = 5  # catalog conversion per ~1,000 tables (dominant)
_MAJOR_PER_JUMP_MIN = 6        # each major version crossed (e.g. 12->16 = 4)
_MAJOR_PER_READER_MIN = 5      # each reader re-upgraded + lag-verified

# Storage's ONLY real influence is the pre/post snapshot scan. Aurora snapshots
# are incremental/fast, so this is deliberately small AND CAPPED — a multi-TB
# volume must never make a (size-independent) minor upgrade look expensive.
_SNAPSHOT_PER_100GB_MIN = 0.5
_SNAPSHOT_CAP_MIN = 10.0

# Background overheads that DON'T add downtime (they overlap production).
_BG_PROVISION_MIN = 15         # blue/green: build green env + start replication
_CLONE_PROVISION_MIN = 5       # fast clone is copy-on-write (near-instant create)

# Object-count fallback when table_stats is empty. We do NOT derive it from
# storage (the whole point is that storage doesn't drive majors); we use a
# neutral default and flag low confidence + a wide range so the number is
# never mistaken for a measurement.
_DEFAULT_OBJECT_COUNT = 1000

# Recommendation thresholds (minor-upgrade branch only; majors always lean
# blue/green). Kept stable so operational guidance is predictable.
_LARGE_STORAGE_GB = 500
_MANY_READERS = 2

_METHODOLOGY_NOTE = (
    "추정치는 객체(테이블) 수·메이저 버전 점프·리더 수 기반 휴리스틱입니다. "
    "실제 시간은 워크로드(MySQL undo/history list length, blue/green 복제 catch-up 시 "
    "쓰기량)에 따라 달라집니다. 정확한 수치가 필요하면 fast clone으로 동일 클러스터를 "
    "복제해 시험 업그레이드를 1회 측정하는 것이 AWS 권장 방식입니다."
)


# --- Standalone RDS MySQL major ladder --------------------------------------
# MySQL's major version is the FIRST TWO components, not the leading integer:
# AWS reports 8.0.42 -> 8.4.6 as ``IsMajorVersionUpgrade: true`` (measured with
# describe-db-engine-versions), yet the leading integer is 8 on both sides. So
# the bare-integer rule that is correct for PostgreSQL called that a MINOR with
# high confidence, and the REST /upgrade-impact route has no engine-family gate,
# so a registered standalone RDS MySQL cluster reached it (measured live).
#
# Aurora MySQL is NOT here: its version string carries "mysql_aurora.<family>",
# which major_family() reads directly and which is what actually distinguishes
# an Aurora MySQL major.
#
# Ordered oldest-first so "families crossed" is a list distance. Two-component
# families cannot use the arithmetic distance _family_int gives: it reads "8.0"
# as 0 and "8.4" as 4, i.e. FOUR majors for one family step. A family missing
# from the ladder (a future MySQL release) falls back to the same safe "1 if
# major" the unparseable case uses, so a stale ladder under-counts rather than
# inventing a distance. Measured available families on RDS today: 5.7, 8.0, 8.4.
_MYSQL_MAJOR_LADDER = ("5.5", "5.6", "5.7", "8.0", "8.4")


def _is_plain_mysql(engine) -> bool:
    """True for standalone RDS MySQL (``cluster_meta.engine == "mysql"``).

    False for Aurora MySQL (``"aurora-mysql"``), whose version string encodes
    the major as ``mysql_aurora.<n>`` and is handled by the branch above it.
    Engine is the authoritative signal here: the version text alone cannot tell
    a MySQL ``"8.0.42"`` from a hypothetical PostgreSQL ``"8.0"``.
    """
    text = str(engine or "").strip().lower()
    return "mysql" in text and "aurora" not in text


def major_family(version: str, engine=None) -> str:
    """Best-effort MAJOR engine family token from an engine version string.

    Aurora PostgreSQL ``"15.4"`` / ``"16.2"`` -> the integer before the first
    dot. Aurora MySQL ``"8.0.mysql_aurora.3.06.0"`` -> the aurora family major
    (``"mysql_aurora.3"``), since that is what distinguishes a MySQL major.
    Standalone RDS MySQL (``engine="mysql"``) -> the first TWO components
    (``"8.0.42"`` -> ``"8.0"``, ``"8.4.9"`` -> ``"8.4"``), which is where MySQL
    puts its major boundary. ``engine`` is optional and defaults to the
    pre-existing behaviour, so a caller that does not know the engine is
    unaffected.

    Returns a comparison token, or ``""`` when unparseable (callers treat an
    empty token as "cannot prove it's a minor" => major).
    """
    if not version:
        return ""
    text = str(version).strip().lower()
    if "mysql_aurora." in text:
        tail = text.split("mysql_aurora.", 1)[1]  # e.g. "3.06.0"
        family = tail.split(".", 1)[0]            # e.g. "3"
        return f"mysql_aurora.{family}" if family else ""
    if _is_plain_mysql(engine):
        # "8.4.9" -> "8.4"; "5.7.44-rds.20250213" -> "5.7". A single component
        # ("8") cannot name a MySQL family, so it stays unparseable => major.
        parts = text.split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return f"{int(parts[0])}.{int(parts[1])}"
        return ""
    head = text.split(".", 1)[0]
    return head if head.isdigit() else ""


def _family_int(family: str):
    """Trailing integer of a family token: ``"15"`` -> 15, ``"mysql_aurora.3"``
    -> 3, anything else -> ``None``."""
    if not family:
        return None
    token = family.rsplit(".", 1)[-1]
    return int(token) if token.isdigit() else None


def _family_kind(family: str):
    """``"mysql_aurora"`` for an Aurora-MySQL family token, ``"pg"`` for a bare
    integer (PostgreSQL), ``None`` if empty. Used to avoid comparing a MySQL
    family number against a PostgreSQL one (e.g. ``mysql_aurora.2`` vs ``"8"``)."""
    if not family:
        return None
    return "mysql_aurora" if family.startswith("mysql_aurora") else "pg"


def classify_upgrade(current_version: str, target_version: str, engine=None) -> str:
    """``"major"`` if the major family changes, else ``"minor"``.

    Unparseable inputs are treated as ``"major"`` so guidance stays on the
    safer, higher-effort path by default. Pass ``engine`` (cluster_meta.engine)
    whenever the caller has it: it is what makes a standalone RDS MySQL
    ``8.0 -> 8.4`` read as the major AWS says it is.
    """
    cur = major_family(current_version, engine)
    tgt = major_family(target_version, engine)
    if not cur or not tgt:
        return "major"
    return "minor" if cur == tgt else "major"


def major_jump(current_version: str, target_version: str, engine=None) -> int:
    """Number of major versions crossed (``12 -> 16`` = 4, ``15.4 -> 15.7`` = 0).

    Falls back to 1 when the change is a major but the distance can't be
    computed (so the major uplift term is never zeroed out for a real major).
    A downgrade floors at 0, matching the pre-existing convention.
    """
    cur_family = major_family(current_version, engine)
    tgt_family = major_family(target_version, engine)
    if _is_plain_mysql(engine):
        # Ladder distance, not arithmetic: see _MYSQL_MAJOR_LADDER.
        if cur_family in _MYSQL_MAJOR_LADDER and tgt_family in _MYSQL_MAJOR_LADDER:
            return max(
                _MYSQL_MAJOR_LADDER.index(tgt_family)
                - _MYSQL_MAJOR_LADDER.index(cur_family),
                0,
            )
        return 0 if classify_upgrade(current_version, target_version, engine) == "minor" else 1
    cur = _family_int(cur_family)
    tgt = _family_int(tgt_family)
    # Only a NUMERIC distance when both sides are the same engine kind; a
    # PostgreSQL "16" vs Aurora-MySQL "mysql_aurora.2" comparison is meaningless
    # and would otherwise yield an absurd jump (e.g. 16 - 2). Fall back to the
    # safe "1 if major else 0" in that case.
    if cur is None or tgt is None or _family_kind(cur_family) != _family_kind(tgt_family):
        return 0 if classify_upgrade(current_version, target_version) == "minor" else 1
    return max(tgt - cur, 0)


def _confidence_and_range(upgrade_type: str, object_count_known: bool):
    """(confidence, low_mult, high_mult) for the estimate range.

    Minors are predictable (tight, high confidence). Majors are inherently
    variable; an unknown object count widens the range and drops confidence so
    the UI can flag it.
    """
    if upgrade_type == "minor":
        return "high", 0.7, 1.5
    if object_count_known:
        return "medium", 0.6, 1.8
    return "low", 0.5, 2.5


def _core_minutes(upgrade_type, object_count, major_jumps, readers, storage_gb):
    """Method-independent core compute time (minutes) + the per-class basis."""
    storage_term = min((max(storage_gb, 0.0) / 100.0) * _SNAPSHOT_PER_100GB_MIN, _SNAPSHOT_CAP_MIN)
    if upgrade_type == "minor":
        core = _MINOR_WRITER_MIN + readers * _MINOR_PER_READER_MIN + storage_term
        basis = [
            f"마이너 업그레이드(바이너리 교체, 데이터 파일 미변경) — writer 재시작 "
            f"{_MINOR_WRITER_MIN}분 기준, 데이터 크기·객체 수와 거의 무관",
        ]
        if readers:
            basis.append(f"리더 {readers}개 패치 (+{readers * _MINOR_PER_READER_MIN}분)")
        return core, basis

    object_term = (max(object_count, 1) / 1000.0) * _MAJOR_PER_1K_OBJECTS_MIN
    jump_term = max(major_jumps, 1) * _MAJOR_PER_JUMP_MIN
    reader_term = readers * _MAJOR_PER_READER_MIN
    core = _MAJOR_BASE_MIN + object_term + jump_term + reader_term + storage_term
    basis = [
        f"메이저 업그레이드 — pg_upgrade/precheck의 catalog 변환이 지배적이며 "
        f"객체(테이블) 수 ~{int(object_count)}개 기반 (+{round(object_term)}분)",
        f"메이저 버전 {major_jumps}단계 점프 (+{round(jump_term)}분)",
    ]
    if readers:
        basis.append(f"리더 {readers}개 재업그레이드·검증 (+{reader_term}분)")
    return core, basis


def _method_estimate(method, upgrade_type, core, object_term_only, basis_common):
    """Per-method wall-clock + downtime, given the shared core compute time.

    ``object_term_only`` is the writer-offline portion of a MAJOR in-place
    upgrade (base + object + jump, excluding background/reader terms); unused
    for minors.
    """
    risk = {"in_place": "moderate", "blue_green": "low", "clone": "medium"}[method]
    basis = list(basis_common)

    if method == "blue_green":
        total = core + _BG_PROVISION_MIN
        downtime_text = "~1분 미만 (switchover)"
        # Switchover is guardrail-bounded to well under a minute (~30 s in the
        # AWS Wiz case study); keep this sub-minute to match the text.
        downtime_seconds = 30
        basis.append(
            "blue/green: green 환경 동기화는 백그라운드(프로덕션 영향 없음), 다운타임은 "
            "switchover(<1분, 가드레일 강제)만 — green 복제 catch-up은 쓰기량에 비례"
        )
    elif method == "clone":
        total = core + _CLONE_PROVISION_MIN
        downtime_text = "~1-2분 (엔드포인트 전환)"
        downtime_seconds = 120
        basis.append(
            "clone: fast clone(copy-on-write)으로 즉시 생성, 원본 무영향; 다운타임은 "
            "애플리케이션 엔드포인트 전환만"
        )
    else:  # in_place
        total = core
        if upgrade_type == "minor":
            downtime_min = _MINOR_WRITER_MIN
            downtime_text = f"~{round(downtime_min)}분 (writer 재시작)"
        else:
            downtime_min = object_term_only
            downtime_text = f"~{round(downtime_min)}분 (pg_upgrade 동안 writer 중단)"
        downtime_seconds = int(round(downtime_min * 60))
        basis.append("in-place: writer가 업그레이드 컴퓨트 창 동안 오프라인")

    return {
        "method": method,
        "estimated_minutes": int(round(total)),
        "downtime_text": downtime_text,
        "downtime_seconds": downtime_seconds,
        "risk": risk,
        "basis": basis,
    }


def recommend_method(upgrade_type: str, storage_gb: float, readers: int) -> tuple[str, str]:
    """Recommend an upgrade method + a Korean reason.

    - MAJOR  => blue/green (in-place major = long downtime + incompat risk).
    - MINOR on a large volume or many readers => blue/green (blast radius).
    - MINOR small => in_place (fast and operationally simple).
    """
    if upgrade_type == "major":
        return "blue_green", (
            "메이저 업그레이드는 in-place 시 다운타임이 길고 비호환 위험이 커 "
            "blue/green 무중단 전환을 권장합니다."
        )
    large = storage_gb >= _LARGE_STORAGE_GB
    many = readers >= _MANY_READERS
    if large or many:
        trig = []
        if large:
            trig.append(f"스토리지 {int(storage_gb)}GB(≥{_LARGE_STORAGE_GB}GB)")
        if many:
            trig.append(f"리더 {readers}개(≥{_MANY_READERS})")
        return "blue_green", (
            f"마이너 업그레이드이지만 {', '.join(trig)} 규모로 in-place 다운타임/"
            "영향 범위가 커 blue/green을 권장합니다."
        )
    return "in_place", (
        f"마이너 업그레이드이고 스토리지 {int(storage_gb)}GB·리더 {readers}개로 규모가 "
        "작아 빠르고 단순한 in-place가 적절합니다."
    )


def estimate_upgrade(
    *,
    engine: str,
    current_version: str,
    target_version: str,
    storage_gb: float,
    readers: int,
    table_count=None,
) -> dict:
    """Full, calibrated upgrade estimate shared by all four call sites.

    Args:
        engine: cluster_meta.engine (``"aurora-postgresql"`` / ``"aurora-mysql"``
            / ``"mysql"``). Also decides where the MySQL major boundary sits.
        current_version / target_version: engine version strings.
        storage_gb: cluster storage (drives only the small snapshot term).
        readers: live reader count.
        table_count: object-count proxy from ``table_stats`` (None if unknown).

    Returns a dict carrying ``upgrade_type``, ``major_jump``, ``table_count``,
    ``object_count_basis``, ``confidence``, per-method ``methods`` (each with
    ``estimated_minutes`` + ``range_low_minutes`` / ``range_high_minutes`` +
    ``downtime_text`` / ``downtime_seconds`` + ``risk`` + ``basis``), a
    ``recommendation`` (+ reason), and a ``methodology_note``.
    """
    # engine is passed through: standalone RDS MySQL puts its major boundary at
    # the second component, so without it 8.0 -> 8.4 classifies as a minor.
    upgrade_type = classify_upgrade(current_version, target_version, engine)
    jumps = major_jump(current_version, target_version, engine)
    readers = max(int(readers or 0), 0)
    try:
        storage_gb = float(storage_gb)
    except (TypeError, ValueError):
        storage_gb = 50.0

    # Coerce defensively: a non-numeric table_count must degrade to "unknown"
    # (default + low confidence), never raise mid-estimate.
    try:
        table_count = int(table_count) if table_count is not None else None
    except (TypeError, ValueError):
        table_count = None
    object_count_known = table_count is not None and upgrade_type == "major"
    object_count = table_count if table_count is not None else _DEFAULT_OBJECT_COUNT
    if upgrade_type == "major":
        object_count_basis = (
            f"table_stats 라이브 객체 수 {object_count}개"
            if table_count is not None
            else "객체 수 미상 (table_stats 미수집) — 기본값으로 추정, 신뢰도 낮음"
        )
    else:
        object_count_basis = "마이너 업그레이드는 객체 수 무관"

    confidence, low_mult, high_mult = _confidence_and_range(upgrade_type, object_count_known)

    core, basis_common = _core_minutes(upgrade_type, object_count, jumps, readers, storage_gb)
    # Writer-offline portion of a MAJOR in-place upgrade (no background/reader terms).
    if upgrade_type == "major":
        object_term_only = (
            _MAJOR_BASE_MIN
            + (max(object_count, 1) / 1000.0) * _MAJOR_PER_1K_OBJECTS_MIN
            + max(jumps, 1) * _MAJOR_PER_JUMP_MIN
        )
    else:
        object_term_only = _MINOR_WRITER_MIN

    methods = []
    for method in ("in_place", "blue_green", "clone"):
        m = _method_estimate(method, upgrade_type, core, object_term_only, basis_common)
        m["range_low_minutes"] = int(round(m["estimated_minutes"] * low_mult))
        m["range_high_minutes"] = int(round(m["estimated_minutes"] * high_mult))
        methods.append(m)

    recommendation, reason = recommend_method(upgrade_type, storage_gb, readers)

    return {
        "upgrade_type": upgrade_type,
        "major_jump": jumps,
        "engine": engine or "aurora-postgresql",
        "storage_gb": storage_gb,
        "readers": readers,
        "table_count": table_count,
        "object_count_basis": object_count_basis,
        "confidence": confidence,
        "methods": methods,
        "recommendation": recommendation,
        "recommendation_reason": reason,
        "methodology_note": _METHODOLOGY_NOTE,
    }
