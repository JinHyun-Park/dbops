from mcp_servers.shared.cache_client import CacheClient

PARAMETER_INFO = {
    "shared_buffers": {"type": "static", "impact": "memory", "restart": True},
    "work_mem": {"type": "dynamic", "impact": "memory", "restart": False},
    "maintenance_work_mem": {"type": "dynamic", "impact": "memory", "restart": False},
    "max_connections": {"type": "static", "impact": "connections", "restart": True},
    "effective_cache_size": {"type": "dynamic", "impact": "planner", "restart": False},
    "innodb_buffer_pool_size": {"type": "static", "impact": "memory", "restart": True},
    "innodb_lock_wait_timeout": {"type": "dynamic", "impact": "locking", "restart": False},
    "long_query_time": {"type": "dynamic", "impact": "logging", "restart": False},
}


def simulate_parameter_change_impl(cache: CacheClient, cluster_id: str, parameter_name: str, new_value: str) -> dict:
    info = PARAMETER_INFO.get(parameter_name, {"type": "unknown", "impact": "unknown", "restart": False})
    return {
        "cluster_id": cluster_id,
        "parameter": parameter_name,
        "new_value": new_value,
        "is_dynamic": info["type"] == "dynamic",
        "requires_restart": info["restart"],
        "impact_area": info["impact"],
        "recommendation": "즉시 적용 가능" if not info["restart"] else "재시작 필요 — 점검 윈도우에서 수행 권장",
    }
