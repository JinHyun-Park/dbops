from mcp_servers.shared.cache_client import CacheClient

ACU_PRICING = {"writer": 0.12, "reader": 0.06}


def simulate_scaling_impl(cache: CacheClient, cluster_id: str, new_min_acu: float = None, new_max_acu: float = None) -> dict:
    meta_sql = "SELECT * FROM cluster_meta WHERE cluster_id = :cluster_id"
    meta = cache.execute(meta_sql, {"cluster_id": cluster_id})
    cluster = meta.rows[0] if meta.rows else {}

    current_cost = 2.0 * ACU_PRICING["writer"] * 730
    new_min = new_min_acu or 0.5
    new_max = new_max_acu or 4.0
    estimated_cost = ((new_min + new_max) / 2) * ACU_PRICING["writer"] * 730

    return {
        "cluster_id": cluster_id,
        "current": {"min_acu": 0.5, "max_acu": 4.0},
        "proposed": {"min_acu": new_min, "max_acu": new_max},
        "cost_impact": {
            "current_monthly_estimate": f"${current_cost:.2f}",
            "proposed_monthly_estimate": f"${estimated_cost:.2f}",
            "change": f"{'+'if estimated_cost > current_cost else ''}{((estimated_cost-current_cost)/current_cost*100):.1f}%",
        },
        "notes": "ACU 변경은 즉시 적용되며 다운타임 없음",
    }
