"""audit_permissions: list login-capable DB users and flag superusers.

ENGINE RESOLUTION IS NOT A CALLER ARGUMENT
------------------------------------------
This tool used to take `engine: str = "postgresql"` and branch on it, so the
dialect was decided by a DEFAULT rather than by the cluster. Measured live on
2026-08-02 against a real Aurora MySQL cluster: the PostgreSQL branch ran and the
target answered `Table 'sampledb.pg_roles' doesn't exist` (MySQL error 1146),
surfacing as a generic "실행이 실패했습니다". The MySQL branch existed the whole
time and was never selected, because reaching it required the caller (an LLM) to
know the engine and pass it.

The engine now comes from `cluster_meta`, the same source every other tool uses.
An explicit `engine` argument is still honoured as an override.

WHY THE GATE IS ON sql_via AND NOT ON THE `sql` CAPABILITY
----------------------------------------------------------
`cache.execute_on_target` is Data-API-only. `rds_instance` HAS the `sql`
capability but reaches SQL over direct TCP, so a Data-API call for it resolves no
cluster_arn and returns an empty QueryResult. The old code read that emptiness as
"cluster not registered or unreachable — register via /clusters" and said so for
five perfectly registered clusters, which sends an operator to re-register
something already correct. `sql_via == "data_api"` is the predicate that actually
decides whether this code path can work.
"""

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.engine_family import CAPABILITIES, engine_family

_PG_SQL = """
    SELECT rolname as username, rolsuper as is_superuser, rolcreatedb as can_create_db,
           rolcreaterole as can_create_role, rolcanlogin as can_login
    FROM pg_roles WHERE rolcanlogin = true ORDER BY rolname
"""

_MYSQL_SQL = """
    SELECT User as username, Host as host,
           IF(Super_priv='Y','true','false') as is_superuser,
           IF(Grant_priv='Y','true','false') as can_grant
    FROM mysql.user ORDER BY User
"""


def _cluster_engine(cache: CacheClient, cluster_id: str) -> str:
    """The engine string from cluster_meta, or "" when it cannot be read."""
    try:
        res = cache.execute(
            "SELECT engine FROM cluster_meta WHERE cluster_id = :cid",
            {"cid": cluster_id},
        )
    except Exception:
        return ""
    # cache.execute returns a QueryResult, not a list.
    rows = getattr(res, "rows", res)
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return str(rows[0].get("engine") or "")
    return ""


def audit_permissions_impl(cache: CacheClient, cluster_id: str, engine: str = "") -> dict:
    # The FAMILY always comes from cluster_meta; the caller's `engine` overrides
    # only the DIALECT. Deriving the family from the override instead would refuse
    # a legitimate call: engine_family("mysql") is rds_instance (bare MySQL), so a
    # caller passing engine="mysql" to mean Aurora MySQL would be told its own
    # engine is unsupported. The cluster row knows it is "aurora-mysql"; the
    # caller only knows which SQL dialect it wants.
    registered = _cluster_engine(cache, cluster_id)
    fam = engine_family(registered)
    resolved = engine or registered

    # Never GUESS the dialect. engine_family() classifies "", None and any unknown
    # string as "relational" (its documented default-permit), so an unresolvable
    # cluster would sail past the gate below and then pick a dialect from
    # `"postgres" in ""` == False, i.e. it would silently run the MySQL query on a
    # cluster whose engine nobody could read. Guessing a dialect is exactly what
    # produced the pg_roles-on-MySQL failure this rewrite fixes, so say so instead.
    if not resolved:
        return {
            "status": "engine_unresolved",
            "cluster_id": cluster_id,
            "engine": "",
            "engine_family": fam,
            "reason": (
                "cluster_meta에서 이 클러스터의 엔진을 읽을 수 없어 SQL 다이얼렉트를 "
                "결정할 수 없습니다. 추측하면 잘못된 카탈로그를 조회하므로 중단합니다. "
                "engine 인자로 명시하거나(postgresql/mysql), /clusters에서 등록 정보를 "
                "확인하세요."
            ),
            "users": [],
            "total_users": 0,
            "superuser_count": 0,
            "warnings": [],
        }

    # Refuse cleanly rather than run a query that cannot reach this engine.
    if CAPABILITIES.get(fam, {}).get("sql_via") != "data_api":
        return {
            "status": "unsupported_engine",
            "cluster_id": cluster_id,
            "engine": resolved or "unknown",
            "engine_family": fam,
            "reason": (
                "사용자·권한 감사는 RDS Data API로 조회하므로 Aurora(PostgreSQL/MySQL) "
                "전용입니다. 표준 RDS 인스턴스는 Data API가 없어 이 경로로 조회할 수 없고"
                "(execute_sql로 직접 조회하세요), DocumentDB·DynamoDB·ElastiCache는 "
                "SQL 사용자 카탈로그 자체가 없습니다."
            ),
            "users": [],
            "total_users": 0,
            "superuser_count": 0,
            "warnings": [],
        }

    is_pg = "postgres" in resolved.lower()
    sql = _PG_SQL if is_pg else _MYSQL_SQL

    # Execute against the TARGET Aurora cluster, not the cache DB.
    result = cache.execute_on_target(cluster_id, sql)
    if result.row_count == 0 and not result.columns:
        # An Aurora cluster CAN legitimately land here: the registry row is missing
        # cluster_arn or secret_arn, or the Data API is disabled on the cluster.
        # Both are registration-completeness problems, so the message names them
        # instead of claiming the cluster is unknown.
        return {
            "status": "target_unreachable",
            "cluster_id": cluster_id,
            "engine": resolved,
            "reason": (
                "대상 클러스터에 SQL을 실행할 수 없습니다. 레지스트리 행에 cluster_arn "
                "또는 secret_arn이 없거나, 클러스터에서 RDS Data API가 비활성일 수 "
                "있습니다 (/clusters에서 등록 정보를 확인하세요)."
            ),
            "users": [],
            "total_users": 0,
            "superuser_count": 0,
            "warnings": [],
        }

    superusers = [r for r in result.rows if r.get("is_superuser") in (True, "true", "t", 1)]
    return {
        "status": "ok",
        "cluster_id": cluster_id,
        "engine": resolved,
        "dialect": "postgresql" if is_pg else "mysql",
        "users": result.rows,
        "total_users": result.row_count,
        "superuser_count": len(superusers),
        "warnings": [f"User '{u.get('username')}' has superuser privileges" for u in superusers],
    }
