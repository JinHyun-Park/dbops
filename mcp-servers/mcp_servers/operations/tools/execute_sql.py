import os
import re

import boto3

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient

SAFE_PATTERNS = [r"^\s*SELECT\b", r"^\s*EXPLAIN\b", r"^\s*SHOW\b", r"^\s*DESCRIBE\b"]
DANGEROUS_PATTERNS = [r"\bDROP\b", r"\bTRUNCATE\b", r"\bDELETE\s+FROM\b"]

_CLUSTERS_TABLE_NAME = os.environ.get("CLUSTERS_TABLE", "")


def _lookup_cluster(cluster_id: str) -> dict:
    """Resolve cluster_arn / secret_arn / db_name from the DynamoDB clusters
    registry. Returns {} if not found or if the table is not configured."""
    if not cluster_id:
        return {}
    if not _CLUSTERS_TABLE_NAME:
        return {}
    try:
        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(_CLUSTERS_TABLE_NAME)
        resp = table.get_item(Key={"cluster_id": cluster_id})
        return resp.get("Item") or {}
    except Exception as e:
        print(f"[execute_sql] cluster lookup failed for {cluster_id}: {e}")
        return {}


def execute_sql_impl(
    cache: CacheClient,
    cluster_id: str,
    sql: str,
    approved: bool = False,
    force: bool = False,
    approval_id: str = "",
) -> dict:
    sql_upper = sql.strip().upper()
    is_safe = any(re.match(p, sql_upper) for p in SAFE_PATTERNS)
    is_dangerous = any(re.search(p, sql_upper) for p in DANGEROUS_PATTERNS)

    if is_dangerous and not force:
        return {"status": "blocked", "reason": "Dangerous SQL (DROP/TRUNCATE/DELETE) requires force=true", "sql": sql}

    if not is_safe and not approved:
        return {"status": "approval_required", "reason": "Non-SELECT SQL requires DBA approval", "sql": sql}

    # Server-side approval enforcement: a write tool that claims approved=true
    # must back it up with a verifiable approval_id. The guard refuses
    # mismatched cluster, stale/replayed approvals, and unapproved rows.
    if not is_safe and approved:
        guard = verify_approval(approval_id, cluster_id, "execute_sql")
        if not guard.get("ok"):
            return {
                "status": "approval_denied",
                "reason": guard.get("reason", "approval guard rejected the request"),
                "sql": sql,
            }

    # Resolve target cluster ARN/Secret from the DynamoDB clusters registry.
    # Falls back to env-var TARGET_* for legacy single-cluster deployments.
    cluster = _lookup_cluster(cluster_id)
    target_arn = cluster.get("cluster_arn") or os.environ.get("TARGET_CLUSTER_ARN", "")
    target_secret = cluster.get("secret_arn") or os.environ.get("TARGET_SECRET_ARN", "")
    target_db = cluster.get("db_name") or os.environ.get("TARGET_DB_NAME", "")

    if not target_arn or not target_secret:
        return {
            "status": "no_target",
            "reason": f"cluster_id={cluster_id!r} not found in registry — register it via /clusters first",
            "registry_table": _CLUSTERS_TABLE_NAME,
        }

    rds_data = boto3.client("rds-data")
    try:
        resp = rds_data.execute_statement(
            resourceArn=target_arn,
            secretArn=target_secret,
            database=target_db,
            sql=f"/* source=dbops-agent */ {sql}",
            includeResultMetadata=True,
        )
    except Exception as e:
        return {
            "status": "execution_failed",
            "error": str(e),
            "cluster_id": cluster_id,
            "target_arn": target_arn,
        }

    cols = [c["name"] for c in resp.get("columnMetadata", [])]
    rows = []
    for rec in resp.get("records", []):
        row = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) else f"col_{i}"
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in f:
                    row[col] = f[typ]
                    break
            else:
                row[col] = None
        rows.append(row)
    return {
        "status": "executed",
        "cluster_id": cluster_id,
        "columns": cols,
        "rows": rows,
        "row_count": len(rows),
    }
