from mcp_servers.shared.cache_client import CacheClient

UPGRADE_ESTIMATES = {
    "in_place": {"base_minutes": 20, "per_100gb": 5, "downtime_minutes": 8},
    "blue_green": {"base_minutes": 30, "per_100gb": 8, "downtime_seconds": 30},
    "clone": {"base_minutes": 15, "per_100gb": 3, "downtime_minutes": 1},
}


def estimate_upgrade_impact_impl(cache: CacheClient, cluster_id: str, target_version: str) -> dict:
    meta_sql = "SELECT storage_size_gb, engine_version FROM cluster_meta WHERE cluster_id = :cluster_id"
    meta = cache.execute(meta_sql, {"cluster_id": cluster_id})
    cluster = meta.rows[0] if meta.rows else {}
    storage_gb = float(cluster.get("storage_size_gb", 50))

    methods = []
    for method, est in UPGRADE_ESTIMATES.items():
        total_min = est["base_minutes"] + (storage_gb / 100) * est["per_100gb"]
        downtime = est.get("downtime_minutes", est.get("downtime_seconds", 0) / 60)
        risk = "low" if method == "blue_green" else "medium" if method == "clone" else "moderate"
        methods.append({
            "method": method,
            "estimated_minutes": round(total_min),
            "downtime": f"~{int(downtime)}분" if downtime >= 1 else f"~{int(est.get('downtime_seconds', 30))}초",
            "risk": risk,
        })

    return {
        "cluster_id": cluster_id,
        "current_version": cluster.get("engine_version", "unknown"),
        "target_version": target_version,
        "storage_gb": storage_gb,
        "methods": methods,
        "recommendation": "blue_green",
    }
