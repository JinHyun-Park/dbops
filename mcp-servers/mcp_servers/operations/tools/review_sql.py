import re

from mcp_servers.shared.cache_client import CacheClient

RISK_LEVELS = {"SELECT": "safe", "INSERT": "low", "UPDATE": "medium", "DELETE": "high", "ALTER": "high", "DROP": "critical", "TRUNCATE": "critical", "CREATE": "medium"}


def review_sql_impl(cache: CacheClient, cluster_id: str, sql: str) -> dict:
    sql_upper = sql.strip().upper()
    first_word = sql_upper.split()[0] if sql_upper.split() else ""
    risk = RISK_LEVELS.get(first_word, "unknown")

    issues = []
    if re.search(r"\bDELETE\s+FROM\b", sql_upper) and "WHERE" not in sql_upper:
        issues.append("DELETE without WHERE clause — will delete all rows")
    if re.search(r"\bUPDATE\b", sql_upper) and "WHERE" not in sql_upper:
        issues.append("UPDATE without WHERE clause — will update all rows")
    if re.search(r"\bDROP\b", sql_upper):
        issues.append("DROP is irreversible")
    if re.search(r"\bTRUNCATE\b", sql_upper):
        issues.append("TRUNCATE is irreversible")

    rollback = None
    if first_word == "ALTER" and "ADD COLUMN" in sql_upper:
        col = re.search(r"ADD\s+COLUMN\s+(\w+)", sql_upper)
        if col:
            table = re.search(r"ALTER\s+TABLE\s+(\w+)", sql_upper)
            if table:
                rollback = f"ALTER TABLE {table.group(1)} DROP COLUMN {col.group(1)}"

    return {
        "cluster_id": cluster_id, "sql": sql, "risk_level": risk,
        "issues": issues, "rollback_sql": rollback,
        "recommendation": "safe to execute" if not issues else "review issues before executing",
    }
