"""review_sql — pre-execution SQL risk review.

Two correctness fixes over the old first-word/raw-text version:

1. **Structure, not data.** Risk + issue detection run on a literal/comment-
   stripped copy (shared :mod:`sql_safety` scanner), so a benign
   ``... WHERE note = 'DROP the table'`` no longer reads as a DROP, and a
   ``'a;b'`` literal no longer looks multi-statement.

2. **No data-loss "rollback" advice.** The old code suggested
   ``DROP COLUMN`` as the rollback for ``ADD COLUMN`` — but by the time you
   roll back, the new column may hold data, so that "rollback" destroys it. We
   now emit ``rollback_sql`` ONLY for genuinely reversible operations
   (CREATE INDEX → DROP INDEX, RENAME → reverse RENAME); for ADD COLUMN we
   return no auto-rollback and a caution note instead.
"""

import re

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.sql_safety import (
    SIDE_EFFECTING_PATTERNS,
    is_multi_statement,
    strip_sql_literals,
)

# Severity by leading command, plus a precedence rank so we can escalate.
RISK_LEVELS = {
    "SELECT": "safe",
    "SHOW": "safe",
    "EXPLAIN": "safe",
    "INSERT": "low",
    "CREATE": "medium",
    "UPDATE": "medium",
    "DELETE": "high",
    "ALTER": "high",
    "GRANT": "high",
    "REVOKE": "high",
    "DROP": "critical",
    "TRUNCATE": "critical",
}
_RANK = {"safe": 0, "low": 1, "medium": 2, "high": 3, "critical": 4, "unknown": 2}


def _safe_rollback(sql: str):
    """Return (rollback_sql, rollback_note) — but only emit rollback_sql for
    operations that reverse WITHOUT data loss. Matches the ORIGINAL sql
    (case-insensitive) so identifier case is preserved in the suggestion."""
    su = sql.upper()

    # CREATE [UNIQUE] INDEX [CONCURRENTLY] name ON ...  →  DROP INDEX name.
    # An index holds no user data, so dropping it is a clean reversal.
    m = re.search(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)",
        sql,
        re.IGNORECASE,
    )
    if m:
        concurrently = "CONCURRENTLY " if "CONCURRENTLY" in su else ""
        return f"DROP INDEX {concurrently}{m.group(1)}", None

    # ALTER TABLE t RENAME COLUMN a TO b  →  reverse the rename (no data loss).
    m = re.search(
        r"ALTER\s+TABLE\s+([\w.\"]+)\s+RENAME\s+COLUMN\s+([\w\"]+)\s+TO\s+([\w\"]+)",
        sql,
        re.IGNORECASE,
    )
    if m:
        return f"ALTER TABLE {m.group(1)} RENAME COLUMN {m.group(3)} TO {m.group(2)}", None

    # ALTER TABLE t RENAME TO u  →  reverse.
    m = re.search(
        r"ALTER\s+TABLE\s+([\w.\"]+)\s+RENAME\s+TO\s+([\w.\"]+)",
        sql,
        re.IGNORECASE,
    )
    if m:
        return f"ALTER TABLE {m.group(2)} RENAME TO {m.group(1)}", None

    # ADD COLUMN: deliberately NO auto-rollback. DROP COLUMN would delete any
    # data written to the column after the change — that is data loss, not a
    # rollback.
    if "ADD COLUMN" in su:
        return None, (
            "자동 롤백 미제공 — DROP COLUMN으로 되돌리면 적용 이후 컬럼에 기록된 데이터가 함께 "
            "삭제됩니다. 되돌리려면 데이터 백업 후 수동으로 처리하세요."
        )

    return None, None


def review_sql_impl(cache: CacheClient, cluster_id: str, sql: str) -> dict:
    # Classify on the literal/comment-stripped form so data never trips keywords.
    stripped = strip_sql_literals(sql)
    stripped_upper = stripped.upper()
    tokens = stripped_upper.split()
    first_word = tokens[0] if tokens else ""

    risk = RISK_LEVELS.get(first_word, "unknown")
    # Escalate: a DROP/TRUNCATE anywhere (e.g. ALTER ... DROP COLUMN) is
    # destructive regardless of the leading keyword.
    if re.search(r"\bDROP\b", stripped_upper) or re.search(r"\bTRUNCATE\b", stripped_upper):
        if _RANK["critical"] > _RANK[risk]:
            risk = "critical"

    issues = []
    if re.search(r"\bDELETE\s+FROM\b", stripped_upper) and "WHERE" not in stripped_upper:
        issues.append("DELETE without WHERE clause — will delete all rows")
    if re.search(r"\bUPDATE\b", stripped_upper) and "WHERE" not in stripped_upper:
        issues.append("UPDATE without WHERE clause — will update all rows")
    if re.search(r"\bDROP\b", stripped_upper):
        issues.append("DROP is irreversible")
    if re.search(r"\bTRUNCATE\b", stripped_upper):
        issues.append("TRUNCATE is irreversible")
    if is_multi_statement(stripped):
        issues.append("multiple statements in one request — review each separately")
    # Side-effect patterns (SELECT ... INTO, FOR UPDATE, pg_terminate_backend …)
    # only matter for statements that LOOK read-only — an INSERT legitimately
    # contains "INTO", so don't flag writes here (they're already risk-rated).
    if first_word in ("SELECT", "EXPLAIN", "SHOW", "WITH") and any(
        re.search(p, stripped_upper) for p in SIDE_EFFECTING_PATTERNS
    ):
        issues.append("appears read-only but contains a side-effecting function or locking read")

    rollback_sql, rollback_note = _safe_rollback(sql)

    result = {
        "cluster_id": cluster_id,
        "sql": sql,
        "risk_level": risk,
        "issues": issues,
        "rollback_sql": rollback_sql,
        "recommendation": "safe to execute" if not issues else "review issues before executing",
    }
    if rollback_note:
        result["rollback_note"] = rollback_note
    return result
