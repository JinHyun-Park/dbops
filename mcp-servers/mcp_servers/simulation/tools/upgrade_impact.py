import logging

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster
from mcp_servers.shared.upgrade_estimator import (
    classify_upgrade,
    estimate_upgrade,
)

logger = logging.getLogger(__name__)

# Back-compat re-exports: upgrade_plan.py and existing tests import these names
# from this module. The real implementations now live in the shared estimator
# so the MCP tools and the REST mirror can never drift.
_classify_upgrade = classify_upgrade


def _resolve_reader_count(cluster_id: str) -> tuple[int, str]:
    """Resolve the live reader count via cross-account-aware RDS describe.

    Counts non-writer members from ``DBClusterMembers``. Wrapped in try/except:
    if the describe fails (cluster not reachable, perms, table unset in tests)
    we degrade to 0 readers and surface a note rather than failing the tool —
    a missing reader count must not block an upgrade estimate.

    The note is interpolated into the response's ``reason``, so it is STATIC:
    an RDS describe error carries the hub account id, the platform role name and
    the cluster ARN, and belongs only in the log.
    """
    try:
        rds = rds_client_for_cluster(cluster_id)
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        members = resp["DBClusters"][0].get("DBClusterMembers", [])
        readers = sum(1 for m in members if not m.get("IsClusterWriter", False))
        return readers, ""
    except Exception:  # pragma: no cover - defensive, exercised via mocks
        logger.warning("reader count lookup failed for %s", cluster_id, exc_info=True)
        return 0, "reader count unavailable (assumed 0)"


def _resolve_table_count(cache: CacheClient, cluster_id: str):
    """Live object-count proxy: distinct tables in the latest table_stats snapshot.

    Object count (tables/indexes/routines) — NOT raw storage — is the dominant
    driver of MAJOR upgrade duration, so we surface it when the ETL has
    collected ``table_stats``. Returns ``None`` (not 0) when unavailable so the
    estimator can flag low confidence instead of pretending the DB is empty.
    """
    sql = (
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT DISTINCT schema_name, table_name FROM table_stats"
        "  WHERE cluster_id = :cluster_id"
        "    AND snapshot_time = ("
        "      SELECT MAX(snapshot_time) FROM table_stats WHERE cluster_id = :cluster_id"
        "    )"
        ") t"
    )
    try:
        res = cache.execute(sql, {"cluster_id": cluster_id})
        if not res.rows:
            return None
        n = res.rows[0].get("n")
        if n is None:
            return None
        n = int(n)
        return n if n > 0 else None
    except Exception as e:  # noqa: BLE001 — table_stats is optional, degrade gracefully
        print(f"[upgrade_impact] table_stats count failed: {e}")
        return None


def estimate_upgrade_impact_impl(cache: CacheClient, cluster_id: str, target_version: str) -> dict:
    """Estimate per-method upgrade impact and recommend a method, data-driven.

    Time is computed by the shared :func:`estimate_upgrade` model: MINOR
    upgrades cost ~a writer reboot (size-independent), MAJOR upgrades scale
    with the live OBJECT COUNT (from ``table_stats``) plus the major-version
    jump and reader topology. The method changes downtime, not just wall-clock
    (blue/green = sub-minute switchover; in-place = the upgrade window). Each
    method carries a range + the estimate's confidence + the factors used.
    """
    meta_sql = (
        "SELECT storage_size_gb, engine_version, engine "
        "FROM cluster_meta WHERE cluster_id = :cluster_id"
    )
    meta = cache.execute(meta_sql, {"cluster_id": cluster_id})
    cluster = meta.rows[0] if meta.rows else {}
    storage_gb = float(cluster.get("storage_size_gb") or 50)
    current_version = cluster.get("engine_version") or "unknown"
    engine = cluster.get("engine") or "aurora-postgresql"

    readers, reader_note = _resolve_reader_count(cluster_id)
    table_count = _resolve_table_count(cache, cluster_id)

    est = estimate_upgrade(
        engine=engine,
        current_version=current_version,
        target_version=target_version,
        storage_gb=storage_gb,
        readers=readers,
        table_count=table_count,
    )

    reason = est["recommendation_reason"]
    if reader_note:
        reason = f"{reason} ({reader_note})"

    return {
        "cluster_id": cluster_id,
        "current_version": current_version,
        "target_version": target_version,
        "engine": engine,
        "upgrade_type": est["upgrade_type"],
        "major_jump": est["major_jump"],
        "storage_gb": storage_gb,
        "readers": readers,
        "table_count": table_count,
        "object_count_basis": est["object_count_basis"],
        "confidence": est["confidence"],
        "methods": est["methods"],
        "recommendation": est["recommendation"],
        "recommendation_reason": reason,
        "methodology_note": est["methodology_note"],
    }
