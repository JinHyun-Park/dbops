from mcp_servers.shared.cache_client import CacheClient


def audit_permissions_impl(cache: CacheClient, cluster_id: str, engine: str = "postgresql") -> dict:
    if engine == "postgresql":
        sql = """
            SELECT rolname as username, rolsuper as is_superuser, rolcreatedb as can_create_db,
                   rolcreaterole as can_create_role, rolcanlogin as can_login
            FROM pg_roles WHERE rolcanlogin = true ORDER BY rolname
        """
    else:
        sql = """
            SELECT User as username, Host as host,
                   IF(Super_priv='Y','true','false') as is_superuser,
                   IF(Grant_priv='Y','true','false') as can_grant
            FROM mysql.user ORDER BY User
        """
    result = cache.execute(sql, {"cluster_id": cluster_id})
    superusers = [r for r in result.rows if r.get("is_superuser") in (True, "true", "t")]
    return {
        "cluster_id": cluster_id, "engine": engine, "users": result.rows,
        "total_users": result.row_count, "superuser_count": len(superusers),
        "warnings": [f"User '{u.get('username')}' has superuser privileges" for u in superusers],
    }
