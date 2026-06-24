"""Saved SQL queries API — durable scratchpad for Query Lab.

Routes:
  GET    /api/saved-queries                 — list (?cluster_id, ?tag, ?limit)
  POST   /api/saved-queries                 — create
  GET    /api/saved-queries/{id}            — fetch one
  PUT    /api/saved-queries/{id}            — update (admin-only)
  DELETE /api/saved-queries/{id}            — delete (admin-only)

The body is plain SQL text. We don't validate SQL syntax here — the
Query Lab editor takes whatever the DBA types and the eventual run
goes through execute_sql which has its own guardrails.
"""

import base64
import json
import os
import re

import boto3
import tenancy

# ---------------------------------------------------------------------------
# Auth helpers — mirror api/runbooks/handler.py
# ---------------------------------------------------------------------------


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _caller_groups(event: dict) -> list[str]:
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return []
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    groups = claims.get("cognito:groups") or []
    return groups if isinstance(groups, list) else []


def _caller_name(event: dict) -> str:
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return "anonymous"
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    return (
        claims.get("preferred_username")
        or claims.get("cognito:username")
        or claims.get("email")
        or "anonymous"
    )


def _is_admin(event: dict) -> bool:
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    if not claims:
        return False
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False
    if groups and "dbops-admin" not in groups:
        return False
    return True


# ---------------------------------------------------------------------------
# Response + DB helpers
# ---------------------------------------------------------------------------


def _resp(status: int, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


_TZ_SUFFIX_RE = re.compile(r"(Z|[+-]\d{2}(:?\d{2})?)$")


def _norm_ts(s):
    """Normalize an RDS Data API timestamp string to unambiguous ISO 8601 UTC.

    The Data API returns TIMESTAMP / TIMESTAMPTZ as a space-separated, tz-less
    string in UTC (e.g. "2026-06-09 10:24:28.123"). The browser's `new Date()`
    parses that space form as LOCAL time, so every rendered timestamp came out
    shifted by the viewer's UTC offset (~9h in KST). Emit "...T...Z" so the
    client parses it as UTC and renders it in local time correctly. Strings
    that already carry a zone/offset are left untouched.
    """
    if not s or not isinstance(s, str):
        return s
    iso = s.replace(" ", "T", 1)
    if _TZ_SUFFIX_RE.search(iso):
        return iso
    return iso + "Z"


def _execute(sql: str, params: dict | None = None) -> list[dict]:
    rds = boto3.client("rds-data")
    sql_params = []
    for k, v in (params or {}).items():
        if v is None:
            sql_params.append({"name": k, "value": {"isNull": True}})
        elif isinstance(v, bool):
            sql_params.append({"name": k, "value": {"booleanValue": v}})
        elif isinstance(v, int):
            sql_params.append({"name": k, "value": {"longValue": v}})
        else:
            sql_params.append({"name": k, "value": {"stringValue": str(v)}})

    resp = rds.execute_statement(
        resourceArn=os.environ["CACHE_DB_CLUSTER_ARN"],
        secretArn=os.environ["CACHE_DB_SECRET_ARN"],
        database=os.environ.get("CACHE_DB_NAME", "dbops"),
        sql=f"/* source=dbops-saved-queries */ {sql}",
        parameters=sql_params,
        includeResultMetadata=True,
    )
    meta = resp.get("columnMetadata", [])
    cols = [c.get("name") or c.get("label") or "" for c in meta]
    # typeName per column, so we normalize ONLY timestamp columns (leaving
    # text that happens to look date-ish untouched).
    col_is_ts = ["timestamp" in (c.get("typeName") or "").lower() for c in meta]
    out: list[dict] = []
    for rec in resp.get("records", []):
        row: dict = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) and cols[i] else f"col_{i}"
            if f.get("isNull"):
                row[col] = None
                continue
            if "arrayValue" in f:
                arr = f["arrayValue"].get("stringValues") or []
                row[col] = list(arr)
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in f:
                    val = f[typ]
                    if typ == "stringValue" and i < len(col_is_ts) and col_is_ts[i]:
                        val = _norm_ts(val)
                    row[col] = val
                    break
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_CLUSTER_ID_RE = re.compile(r"^[a-zA-Z0-9._:\-/]{1,255}$")


def _cluster_item(cluster_id: str) -> dict:
    """Fetch {cluster_id, team_id} from the clusters registry for a single
    cluster. Returns {} on miss or infra error (caller's cluster_visible treats
    missing team_id as default-open)."""
    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not cluster_id or not table_name:
        return {}
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        return table.get_item(Key={"cluster_id": cluster_id}).get("Item") or {}
    except Exception as e:
        print(f"[saved_queries] cluster lookup failed for {cluster_id}: {e}")
        return {}


def _validate_create(body: dict) -> str | None:
    title = (body.get("title") or "").strip()
    if not title or len(title) > 255:
        return "title required (1..255 chars)"
    sql_text = (body.get("sql_text") or "").strip()
    if not sql_text:
        return "sql_text required"
    if len(sql_text) > 100_000:
        return "sql_text too long (100000 char limit)"
    cluster_id = body.get("cluster_id")
    if cluster_id is not None and cluster_id != "":
        if not _CLUSTER_ID_RE.match(str(cluster_id)):
            return "invalid cluster_id"
    tags = body.get("tags") or []
    if not isinstance(tags, list):
        return "tags must be an array"
    if len(tags) > 16:
        return "too many tags (max 16)"
    for t in tags:
        if not isinstance(t, str) or len(t) > 64:
            return "each tag must be a string ≤ 64 chars"
    desc = body.get("description") or ""
    if len(desc) > 500:
        return "description too long (500 char limit)"
    return None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def lambda_handler(event, context):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    )
    path_params = event.get("pathParameters") or {}
    qsp = event.get("queryStringParameters") or {}
    saved_id_str = path_params.get("id")

    if method == "GET" and not saved_id_str:
        return _list(event, qsp)
    if method == "GET" and saved_id_str:
        return _get_one(event, saved_id_str)
    if method == "POST":
        return _create(event)
    if method == "PUT" and saved_id_str:
        return _update(event, saved_id_str)
    if method == "DELETE" and saved_id_str:
        return _delete(event, saved_id_str)
    return _resp(405, {"error": f"method {method} not allowed"})


def _list(event: dict, qsp: dict) -> dict:
    cluster_id = qsp.get("cluster_id")
    tag = qsp.get("tag")
    limit = max(1, min(int(qsp.get("limit", "50")), 200))
    clauses = []
    params: dict = {"lim": limit}
    if cluster_id:
        clauses.append("cluster_id = :cid")
        params["cid"] = cluster_id
    if tag:
        # ANY pattern lets a single tag filter match rows whose tag array
        # contains that tag — no special-case for multi-tag here.
        clauses.append(":tag = ANY(tags)")
        params["tag"] = tag
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = _execute(
        "SELECT id, cluster_id, title, description, tags, created_by, "
        "       created_at, updated_at "
        f"FROM saved_queries {where} ORDER BY updated_at DESC LIMIT :lim",
        params,
    )
    visible = tenancy.visible_set_from_registry(event)
    if visible is not None:
        rows = [r for r in rows if r.get("cluster_id") in visible]
    return _resp(200, {"queries": rows})


def _get_one(event: dict, id_str: str) -> dict:
    try:
        sid = int(id_str)
    except ValueError:
        return _resp(400, {"error": "id must be integer"})
    rows = _execute(
        "SELECT id, cluster_id, title, description, sql_text, tags, "
        "       created_by, created_at, updated_at "
        "FROM saved_queries WHERE id = :id LIMIT 1",
        {"id": sid},
    )
    if not rows:
        return _resp(404, {"error": "not found"})
    row = rows[0]
    if not tenancy.cluster_visible(event, _cluster_item(row.get("cluster_id"))):
        return _resp(403, {"error": "이 클러스터에 대한 접근 권한이 없습니다."})
    return _resp(200, row)


def _create(event: dict) -> dict:
    # P2.4.2 server-side RBAC: saving queries is an admin action.
    if not _is_admin(event):
        return _resp(403, {"error": "admin only"})
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "body must be valid JSON"})
    err = _validate_create(body)
    if err:
        return _resp(400, {"error": err})
    created_by = _caller_name(event)
    cluster_id = body.get("cluster_id") or None  # store NULL for cluster-agnostic
    rows = _execute(
        "INSERT INTO saved_queries "
        "  (cluster_id, title, description, sql_text, tags, created_by) "
        "VALUES "
        "  (:cluster_id, :title, :description, :sql_text, "
        "   string_to_array(:tags_csv, ','), :created_by) "
        "RETURNING id, cluster_id, title, description, sql_text, tags, "
        "          created_by, created_at, updated_at",
        {
            "cluster_id": cluster_id,
            "title": body["title"].strip(),
            "description": (body.get("description") or "").strip(),
            "sql_text": body["sql_text"].strip(),
            "tags_csv": ",".join(t.strip() for t in (body.get("tags") or []) if t.strip()),
            "created_by": created_by,
        },
    )
    return _resp(201, rows[0] if rows else {"error": "insert returned no row"})


def _update(event: dict, id_str: str) -> dict:
    if not _is_admin(event):
        return _resp(403, {"error": "admin only"})
    try:
        sid = int(id_str)
    except ValueError:
        return _resp(400, {"error": "id must be integer"})
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "body must be valid JSON"})
    err = _validate_create(body)
    if err:
        return _resp(400, {"error": err})
    rows = _execute(
        "UPDATE saved_queries SET "
        "  cluster_id = :cluster_id, "
        "  title = :title, "
        "  description = :description, "
        "  sql_text = :sql_text, "
        "  tags = string_to_array(:tags_csv, ','), "
        "  updated_at = NOW() "
        "WHERE id = :id "
        "RETURNING id, cluster_id, title, description, sql_text, tags, "
        "          created_by, created_at, updated_at",
        {
            "id": sid,
            "cluster_id": body.get("cluster_id") or None,
            "title": body["title"].strip(),
            "description": (body.get("description") or "").strip(),
            "sql_text": body["sql_text"].strip(),
            "tags_csv": ",".join(t.strip() for t in (body.get("tags") or []) if t.strip()),
        },
    )
    if not rows:
        return _resp(404, {"error": "not found"})
    return _resp(200, rows[0])


def _delete(event: dict, id_str: str) -> dict:
    if not _is_admin(event):
        return _resp(403, {"error": "admin only"})
    try:
        sid = int(id_str)
    except ValueError:
        return _resp(400, {"error": "id must be integer"})
    rows = _execute(
        "DELETE FROM saved_queries WHERE id = :id RETURNING id",
        {"id": sid},
    )
    if not rows:
        return _resp(404, {"error": "not found"})
    return _resp(200, {"deleted": sid})
