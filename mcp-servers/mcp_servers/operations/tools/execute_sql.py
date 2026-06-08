import base64
import os
import re

import boto3

from mcp_servers.shared.approval_guard import verify_approval
from mcp_servers.shared.cache_client import CacheClient

SAFE_PATTERNS = [r"^\s*SELECT\b", r"^\s*EXPLAIN\b", r"^\s*SHOW\b", r"^\s*DESCRIBE\b"]
DANGEROUS_PATTERNS = [r"\bDROP\b", r"\bTRUNCATE\b", r"\bDELETE\s+FROM\b"]

# Constructs that read like a SELECT/EXPLAIN but actually mutate data or change
# server state — these must NOT take the no-approval read fast-path. A prefix
# match (^SELECT/^EXPLAIN) is intent, not enforcement: PostgreSQL exposes plenty
# of side effects from inside a SELECT. Matching any of these downgrades the
# statement to approval-required so it flows through request_approval.
SIDE_EFFECTING_PATTERNS = [
    r"\bEXPLAIN\b[\s(]+(?:[^;]*\b)?ANALYZE\b",  # EXPLAIN ANALYZE actually runs the stmt
    r"\bINTO\b",                                  # SELECT ... INTO creates a table
    r"\bFOR\s+(?:UPDATE|NO\s+KEY\s+UPDATE|SHARE)\b",  # locking reads
    r"\bNEXTVAL\b", r"\bSETVAL\b",               # sequence mutation
    r"\bSET_CONFIG\b",                            # runtime GUC mutation from a SELECT
    r"\bPG_TERMINATE_BACKEND\b", r"\bPG_CANCEL_BACKEND\b",
    r"\bPG_RELOAD_CONF\b", r"\bPG_ROTATE_LOGFILE\b",
    r"\bPG_SWITCH_WAL\b", r"\bPG_SWITCH_XLOG\b",
    r"\bPG_CREATE_RESTORE_POINT\b", r"\bPG_PROMOTE\b",
    r"\bPG_STAT_RESET\w*\b",
    r"\bPG_\w*ADVISORY\w*\b",                     # all advisory lock/try/unlock/xact variants
    r"\bPG_\w*REPLICATION_SLOT\w*\b",             # create/drop logical/physical slots
    r"\bPG_LOGICAL_EMIT_MESSAGE\b", r"\bPG_REPLICATION_ORIGIN_\w+\b",
    r"\bLO_IMPORT\b", r"\bLO_EXPORT\b",
    r"\bPG_READ_FILE\b", r"\bPG_READ_BINARY_FILE\b",
    r"\bDBLINK\w*\b", r"\bPG_SLEEP\w*\b",
]

# Command keywords that, when they appear AFTER a statement-separating ';',
# indicate a stacked/multi-statement payload. Used to refuse the read
# fast-path for anything carrying a second statement.
_STACKED_STMT_RE = re.compile(
    r";\s*(SELECT|INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|"
    r"COPY|EXPLAIN|WITH|VACUUM|ANALYZE|CALL|DO|SET|MERGE|COMMENT|REINDEX|CLUSTER)\b",
    re.IGNORECASE,
)


def _strip_literals(s: str) -> str:
    """Replace string literals, quoted identifiers, and comments so keyword
    classification reflects SQL STRUCTURE, not data. Without this, a benign
    `SELECT note FROM t WHERE note = 'paid into account'` would match
    `\\bINTO\\b`, and `WHERE label = 'a;b'` would look multi-statement.

    This is a single left-to-right scanner, NOT a sequence of regex replaces:
    interleaving matters. Stripping comments first would let `SELECT '--';
    UPDATE t SET x=1` hide its stacked statement (the `--` lives inside a
    string, but a naive comment regex would erase the rest of the line). The
    scanner recognizes whichever construct STARTS first, so a `--` inside a
    string is data and a quote inside a comment is a comment."""
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        nxt = s[i + 1] if i + 1 < n else ""
        if c == "-" and nxt == "-":                      # line comment
            i += 2
            while i < n and s[i] != "\n":
                i += 1
            out.append(" ")
        elif c == "/" and nxt == "*":                    # block comment
            i += 2
            while i < n and not (s[i] == "*" and i + 1 < n and s[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")
        elif c == "'":                                   # '...' string literal
            i += 1
            while i < n:
                if s[i] == "'":
                    if i + 1 < n and s[i + 1] == "'":    # '' escaped quote
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''")
        elif c == '"':                                   # "..." quoted identifier
            i += 1
            while i < n:
                if s[i] == '"':
                    if i + 1 < n and s[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append('""')
        elif c == "`":                                   # `...` MySQL identifier
            i += 1
            while i < n and s[i] != "`":
                i += 1
            i += 1
            out.append("``")
        elif c == "$":                                   # $tag$...$tag$ dollar-quote (PG)
            m = re.match(r"\$([A-Za-z_]\w*)?\$", s[i:])
            if m:
                tag = m.group(0)
                end = s.find(tag, i + len(tag))
                i = end + len(tag) if end != -1 else n
                out.append(" '' ")
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _is_multi_statement(sql: str) -> bool:
    """True if `sql` carries more than one statement. Callers pass the
    literal-stripped form; we drop a single trailing ';' (the common, harmless
    case) and then look for any ';' followed by a SQL command keyword."""
    body = sql.strip().rstrip(";").strip()
    return bool(_STACKED_STMT_RE.search(body))


def _decode_array(arr: dict):
    """Decode an RDS Data API ArrayValue into a Python list (incl. nesting)."""
    for key in ("stringValues", "longValues", "doubleValues", "booleanValues"):
        if key in arr:
            return list(arr[key])
    if "arrayValues" in arr:
        return [_decode_array(a) for a in arr["arrayValues"]]
    return []


def _decode_field(field: dict):
    """Decode one RDS Data API Field into a Python value. Handles the FULL set,
    not just the four scalar types: explicit SQL NULL (isNull), bytea
    (blobValue → base64 string), and arrays (arrayValue). NUMERIC/DECIMAL come
    back as stringValue from the Data API, so exact precision is preserved by
    keeping the string. The previous decoder collapsed NULL/blob/array — and
    any unrecognized field — to None, silently losing or misrepresenting data
    in diagnostics and audit output."""
    if field.get("isNull"):
        return None
    for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
        if typ in field:
            return field[typ]
    if "blobValue" in field:
        blob = field["blobValue"]
        if isinstance(blob, (bytes, bytearray)):
            return base64.b64encode(bytes(blob)).decode("ascii")
        return str(blob)
    if "arrayValue" in field:
        return _decode_array(field["arrayValue"])
    return None


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
    # Classify on a literal/comment-stripped copy so keyword matching reflects
    # SQL STRUCTURE, not data. The statement actually executed is still the
    # original `sql`.
    sanitized = _strip_literals(sql)
    sql_upper = sanitized.strip().upper()
    has_safe_prefix = any(re.match(p, sql_upper) for p in SAFE_PATTERNS)
    is_side_effecting = any(re.search(p, sql_upper) for p in SIDE_EFFECTING_PATTERNS)
    is_multi = _is_multi_statement(sanitized)
    is_dangerous = any(re.search(p, sql_upper) for p in DANGEROUS_PATTERNS)

    # A statement is read-only "safe" ONLY if it both looks like a read AND
    # carries no side-effecting construct AND is a single statement. A SELECT
    # that calls pg_terminate_backend(), an EXPLAIN ANALYZE (which executes the
    # plan), or a stacked "SELECT 1; UPDATE ..." all fail this and must be
    # approved like any other write.
    is_safe = has_safe_prefix and not is_side_effecting and not is_multi

    if is_dangerous and not force:
        return {"status": "blocked", "reason": "Dangerous SQL (DROP/TRUNCATE/DELETE) requires force=true", "sql": sql}

    if not is_safe and not approved:
        reason = "Non-SELECT SQL requires DBA approval"
        if has_safe_prefix and is_side_effecting:
            reason = (
                "SQL looks read-only but contains a side-effecting/state-changing "
                "construct (e.g. EXPLAIN ANALYZE, SELECT INTO, a locking clause, or "
                "a function like pg_terminate_backend) — DBA approval required"
            )
        elif has_safe_prefix and is_multi:
            reason = "Multiple SQL statements are not allowed on the read path — DBA approval required"
        return {"status": "approval_required", "reason": reason, "sql": sql}

    # Server-side approval enforcement: a write tool that claims approved=true
    # must back it up with a verifiable approval_id. The guard refuses
    # mismatched cluster, stale/replayed approvals, and unapproved rows.
    if not is_safe and approved:
        guard = verify_approval(
            approval_id, cluster_id, "execute_sql", payload={"sql": sql}
        )
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
            row[col] = _decode_field(f)
        rows.append(row)
    return {
        "status": "executed",
        "cluster_id": cluster_id,
        "columns": cols,
        "rows": rows,
        "row_count": len(rows),
    }
