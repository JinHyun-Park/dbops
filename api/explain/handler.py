"""POST /api/explain — runs EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) on a SELECT
against the target cluster and returns the parsed plan tree.

Restricted to SELECT statements to avoid `EXPLAIN ANALYZE INSERT/UPDATE/DELETE`
side effects. Plan rendering happens in the frontend; this endpoint is the
pure data path that bypasses the chat agent for speed.
"""

import json
import os
import re
import time

import boto3

_CLUSTERS_TABLE = os.environ.get("CLUSTERS_TABLE", "")
_MAX_SQL_LEN = 100_000

_CORS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
}


def _decode_jwt_payload(token: str) -> dict:
    """Base64-decode a JWT payload — API Gateway has already verified the
    signature before this Lambda is invoked."""
    try:
        import base64
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        import json as _json
        return _json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _is_admin(event: dict) -> bool:
    """True if the caller's token does not place them in dbops-viewer.
    No token at all = NOT admin (fail-closed)."""
    hdrs = event.get("headers") or {}
    auth = hdrs.get("authorization") or hdrs.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False
    if "dbops-viewer" in groups and "dbops-admin" not in groups:
        return False
    return True


def _resp(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": _CORS, "body": json.dumps(body, default=str)}


def _lookup_cluster(cluster_id: str) -> dict:
    if not cluster_id or not _CLUSTERS_TABLE:
        return {}
    try:
        table = boto3.resource("dynamodb").Table(_CLUSTERS_TABLE)
        return table.get_item(Key={"cluster_id": cluster_id}).get("Item") or {}
    except Exception as e:
        print(f"[explain] cluster lookup failed for {cluster_id}: {e}")
        return {}


def _strip_explain_prefix(sql: str) -> str:
    # If user already wrapped, peel one layer so we control format options.
    return re.sub(r"^\s*EXPLAIN\b[^()]*(?:\([^)]*\))?\s*", "", sql, count=1, flags=re.IGNORECASE)


def _is_select(sql: str) -> bool:
    stripped = sql.strip().rstrip(";").lstrip()
    # Allow CTE-prefixed SELECTs too (WITH ... SELECT).
    head = stripped[:6].upper()
    if head == "SELECT":
        return True
    if head[:4] == "WITH":
        # Cheap check: must end with a SELECT inside the CTE.
        return bool(re.search(r"\bSELECT\b", stripped, re.IGNORECASE))
    return False


def _build_explain_sql(sql: str, engine: str, analyze: bool = True) -> str:
    inner = _strip_explain_prefix(sql).rstrip().rstrip(";")
    if engine.startswith("aurora-mysql") or engine == "mysql":
        # MySQL 8.0+: EXPLAIN FORMAT=JSON returns one row with a JSON string.
        # We deliberately skip ANALYZE — its output isn't JSON-formatted.
        return f"EXPLAIN FORMAT=JSON {inner}"
    # Default: PostgreSQL.
    if analyze:
        return f"EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON) {inner}"
    return f"EXPLAIN (BUFFERS, VERBOSE, FORMAT JSON) {inner}"


def _extract_plan_json(records: list, columns: list) -> dict | list | None:
    if not records:
        return None
    # rds-data returns each cell under a typed key. EXPLAIN ... FORMAT JSON
    # always returns a single column whose stringValue is the JSON.
    first_field = records[0][0] if records[0] else {}
    raw = first_field.get("stringValue")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"[explain] plan JSON parse failed: {e}; raw head={raw[:200]}")
        return None


def lambda_handler(event, context):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod", "POST")
    )
    if method != "POST":
        return _resp(405, {"error": f"method {method} not allowed"})

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return _resp(400, {"error": "invalid JSON body"})

    cluster_id = (body.get("cluster_id") or "").strip()
    sql = (body.get("sql") or "").strip()

    if not cluster_id:
        return _resp(400, {"error": "cluster_id required"})
    if not sql:
        return _resp(400, {"error": "sql required"})
    if len(sql) > _MAX_SQL_LEN:
        return _resp(413, {"error": f"sql too long (>{_MAX_SQL_LEN} chars)"})
    if not _is_select(sql):
        return _resp(400, {"error": "only SELECT (or WITH ... SELECT) is allowed in EXPLAIN"})

    # P2.4.2 server-side RBAC: EXPLAIN ANALYZE touches the live cluster
    # engine directly. Gate to admins, consistent with other live-cluster ops.
    if not _is_admin(event):
        return _resp(403, {"error": "forbidden", "reason": "admin role required"})

    cluster = _lookup_cluster(cluster_id)
    if not cluster:
        return _resp(404, {"error": f"cluster {cluster_id!r} not registered"})

    cluster_arn = cluster.get("cluster_arn")
    secret_arn = cluster.get("secret_arn")
    db_name = cluster.get("db_name") or "postgres"
    engine = cluster.get("engine", "aurora-postgresql")

    if not cluster_arn or not secret_arn:
        return _resp(500, {"error": "cluster registry missing cluster_arn/secret_arn"})

    analyze = bool(body.get("analyze", True))
    explain_sql = _build_explain_sql(sql, engine, analyze)

    rds_data = boto3.client("rds-data")
    t0 = time.time()
    try:
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=db_name,
            sql=f"/* source=dbops-explain */ {explain_sql}",
            includeResultMetadata=True,
        )
    except Exception as e:
        # Distinguish SQL errors (user input) from infrastructure errors
        # (timeouts, IAM, network). DatabaseErrorException is the wrapper
        # rds-data uses for anything Postgres/MySQL itself returned.
        msg = str(e)
        is_db_error = "DatabaseErrorException" in msg or "BadRequestException" in msg
        # Try to pull out just the "ERROR: ..." part the engine returned.
        clean = msg
        m = re.search(r"ERROR:\s*(.+?)(?:;\s*Position:|;\s*SQLState:|$)", msg)
        if m:
            clean = m.group(1).strip()
        if is_db_error:
            return _resp(400, {
                "error": "sql_error",
                "message": clean,
                "engine": engine,
                "explain_sql": explain_sql,
            })
        return _resp(502, {
            "error": "execution_failed",
            "message": clean[:500],
            "engine": engine,
            "explain_sql": explain_sql,
        })
    elapsed_ms = int((time.time() - t0) * 1000)

    columns = [c.get("name") for c in resp.get("columnMetadata", [])]
    records = resp.get("records", [])
    plan = _extract_plan_json(records, columns)

    return _resp(200, {
        "engine": engine,
        "cluster_id": cluster_id,
        "elapsed_ms": elapsed_ms,
        "sql": sql,
        "explain_sql": explain_sql,
        "plan": plan,
        "row_count": len(records),
    })
