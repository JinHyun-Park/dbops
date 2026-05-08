import boto3
import os
import re
from mcp_servers.shared.cache_client import CacheClient

SAFE_PATTERNS = [r"^\s*SELECT\b", r"^\s*EXPLAIN\b", r"^\s*SHOW\b", r"^\s*DESCRIBE\b"]
DANGEROUS_PATTERNS = [r"\bDROP\b", r"\bTRUNCATE\b", r"\bDELETE\s+FROM\b"]


def execute_sql_impl(cache: CacheClient, cluster_id: str, sql: str, approved: bool = False, force: bool = False) -> dict:
    sql_upper = sql.strip().upper()
    is_safe = any(re.match(p, sql_upper) for p in SAFE_PATTERNS)
    is_dangerous = any(re.search(p, sql_upper) for p in DANGEROUS_PATTERNS)

    if is_dangerous and not force:
        return {"status": "blocked", "reason": "Dangerous SQL (DROP/TRUNCATE/DELETE) requires force=true", "sql": sql}

    if not is_safe and not approved:
        return {"status": "approval_required", "reason": "Non-SELECT SQL requires DBA approval", "sql": sql}

    rds_data = boto3.client("rds-data")
    target_arn = os.environ.get("TARGET_CLUSTER_ARN", "")
    target_secret = os.environ.get("TARGET_SECRET_ARN", "")
    target_db = os.environ.get("TARGET_DB_NAME", "")

    resp = rds_data.execute_statement(
        resourceArn=target_arn, secretArn=target_secret, database=target_db,
        sql=f"/* source=dbops-agent */ {sql}", includeResultMetadata=True,
    )
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
    return {"status": "executed", "columns": cols, "rows": rows, "row_count": len(rows)}
