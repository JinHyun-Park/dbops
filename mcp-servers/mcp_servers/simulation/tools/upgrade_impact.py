from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster

# Per-method base estimates. Time is storage-driven (per_100gb) plus a small
# per-reader term added later: each reader instance must be re-created/upgraded
# and re-verified, so more readers => longer wall-clock even at equal storage.
UPGRADE_ESTIMATES = {
    "in_place": {"base_minutes": 20, "per_100gb": 5, "downtime_minutes": 8},
    "blue_green": {"base_minutes": 30, "per_100gb": 8, "downtime_seconds": 30},
    "clone": {"base_minutes": 15, "per_100gb": 3, "downtime_minutes": 1},
}

# A reader adds roughly this many minutes to total wall-clock (provision +
# upgrade + lag-verify of one replica). Conservative flat term per reader.
_MINUTES_PER_READER = 6

# Storage threshold (GB) above which we lean toward blue/green even for a minor
# upgrade — large volumes make in-place downtime/rebuild risk unacceptable.
_LARGE_STORAGE_GB = 500

# Reader count at/above which we lean toward blue/green even for a minor
# upgrade — more readers means a larger blast radius during an in-place change.
_MANY_READERS = 2


def _major(version: str) -> str:
    """Best-effort extraction of the MAJOR engine family from an Aurora version.

    Aurora PostgreSQL versions look like ``"15.4"`` / ``"16.2"`` — the major is
    the integer before the first dot. Aurora MySQL versions look like
    ``"8.0.mysql_aurora.3.06.0"`` — the meaningful major family there is the
    ``aurora`` major (the ``3`` in ``mysql_aurora.3.06.0``), since that is what
    distinguishes a major MySQL upgrade.

    We return a string token so callers only ever compare for equality. If the
    version is empty or unparseable we return ``""``; classification then treats
    that as a MAJOR upgrade (conservative — never silently assume a cheap minor).
    """
    if not version:
        return ""
    text = str(version).strip().lower()
    # Aurora MySQL: anchor on the aurora family token, take the integer after it.
    if "mysql_aurora." in text:
        tail = text.split("mysql_aurora.", 1)[1]  # e.g. "3.06.0"
        family = tail.split(".", 1)[0]            # e.g. "3"
        return f"mysql_aurora.{family}" if family else ""
    # Aurora PostgreSQL (and plain "8.0.x" style): integer before first dot.
    head = text.split(".", 1)[0]
    return head if head.isdigit() else ""


def _classify_upgrade(current_version: str, target_version: str) -> str:
    """Classify an upgrade as ``"major"`` or ``"minor"``.

    A change in the MAJOR family => MAJOR upgrade. Same major with a different
    (higher) minor => MINOR. Anything unparseable is treated as MAJOR so the
    recommendation/plan stays on the safer, higher-effort path by default.
    """
    cur_major = _major(current_version)
    tgt_major = _major(target_version)
    if not cur_major or not tgt_major:
        return "major"  # conservative: cannot prove it's a cheap minor
    return "minor" if cur_major == tgt_major else "major"


def _resolve_reader_count(cluster_id: str) -> tuple[int, str]:
    """Resolve the live reader count via cross-account-aware RDS describe.

    Counts non-writer members from ``DBClusterMembers``. Wrapped in try/except:
    if the describe fails (cluster not reachable, perms, table unset in tests)
    we degrade to 0 readers and surface a note rather than failing the tool —
    a missing reader count must not block an upgrade estimate.
    """
    try:
        rds = rds_client_for_cluster(cluster_id)
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        members = resp["DBClusters"][0].get("DBClusterMembers", [])
        readers = sum(1 for m in members if not m.get("IsClusterWriter", False))
        return readers, ""
    except Exception as e:  # pragma: no cover - defensive, exercised via mocks
        return 0, f"reader count unavailable (assumed 0): {e}"


def estimate_upgrade_impact_impl(cache: CacheClient, cluster_id: str, target_version: str) -> dict:
    """Estimate per-method upgrade impact and recommend a method, data-driven.

    Time is driven by storage (existing behaviour) AND reader count. The
    ``recommendation`` is no longer a constant — it reflects whether this is a
    major vs minor upgrade and the cluster's size/topology.
    """
    meta_sql = "SELECT storage_size_gb, engine_version FROM cluster_meta WHERE cluster_id = :cluster_id"
    meta = cache.execute(meta_sql, {"cluster_id": cluster_id})
    cluster = meta.rows[0] if meta.rows else {}
    storage_gb = float(cluster.get("storage_size_gb", 50))
    current_version = cluster.get("engine_version", "unknown")

    upgrade_type = _classify_upgrade(current_version, target_version)
    readers, reader_note = _resolve_reader_count(cluster_id)

    methods = []
    for method, est in UPGRADE_ESTIMATES.items():
        # Storage term (existing) + per-reader term (new): readers extend the
        # wall-clock because each replica is upgraded/re-verified in turn.
        total_min = (
            est["base_minutes"]
            + (storage_gb / 100) * est["per_100gb"]
            + readers * _MINUTES_PER_READER
        )
        downtime = est.get("downtime_minutes", est.get("downtime_seconds", 0) / 60)
        risk = "low" if method == "blue_green" else "medium" if method == "clone" else "moderate"
        methods.append({
            "method": method,
            "estimated_minutes": round(total_min),
            "downtime": f"~{int(downtime)}분" if downtime >= 1 else f"~{int(est.get('downtime_seconds', 30))}초",
            "risk": risk,
        })

    # Recommendation logic (WHY each branch):
    # - MAJOR upgrades cannot be done in-place without long downtime + higher
    #   incompatibility risk, so always steer to blue/green.
    # - For MINOR upgrades on a SMALL cluster (low storage, few readers) an
    #   in-place change is fast and operationally simple — recommend it.
    # - For MINOR upgrades on a LARGE volume or a topology with many readers,
    #   in-place downtime/blast radius is too costly — lean blue/green.
    large = storage_gb >= _LARGE_STORAGE_GB
    many_readers = readers >= _MANY_READERS
    if upgrade_type == "major":
        recommendation = "blue_green"
        reason = (
            f"메이저 업그레이드({current_version} → {target_version})는 in-place 시 "
            "다운타임이 길고 비호환 위험이 커 blue/green으로 무중단 전환을 권장합니다."
        )
    elif large or many_readers:
        recommendation = "blue_green"
        trigger = []
        if large:
            trigger.append(f"스토리지 {int(storage_gb)}GB(≥{_LARGE_STORAGE_GB}GB)")
        if many_readers:
            trigger.append(f"리더 {readers}개(≥{_MANY_READERS})")
        reason = (
            f"마이너 업그레이드이지만 {', '.join(trigger)} 규모로 in-place 다운타임/"
            "영향 범위가 커 blue/green을 권장합니다."
        )
    else:
        recommendation = "in_place"
        reason = (
            f"마이너 업그레이드이고 스토리지 {int(storage_gb)}GB·리더 {readers}개로 "
            "규모가 작아 빠르고 단순한 in-place가 적절합니다."
        )
    if reader_note:
        reason = f"{reason} ({reader_note})"

    return {
        "cluster_id": cluster_id,
        "current_version": current_version,
        "target_version": target_version,
        "upgrade_type": upgrade_type,
        "storage_gb": storage_gb,
        "readers": readers,
        "methods": methods,
        "recommendation": recommendation,
        "recommendation_reason": reason,
    }
