"""Runbooks API — CRUD over the `runbooks` cache table.

The agent's diagnoses are markdown by default, so we store them verbatim.
Listing is keyed by cluster + recency; a future similarity layer (via the
find_similar_incidents MCP tool) will key off the tags column.

Routes:
  GET    /api/runbooks                 — list (optional ?cluster_id, ?tag, ?limit)
  POST   /api/runbooks                 — create
  GET    /api/runbooks/{id}            — fetch one
  DELETE /api/runbooks/{id}            — admin-only delete
"""

import base64
import json
import os
import re
import traceback

import boto3

# ---------------------------------------------------------------------------
# Auth helpers (mirror api/alerts/handler.py — DBOps role gate)
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
            "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _rds_data():
    return boto3.client("rds-data")


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
    rds = _rds_data()
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
        sql=f"/* source=dbops-runbooks */ {sql}",
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
                # Postgres text[] comes back as nested {stringValues: [...]}
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
_VALID_SOURCES = {"chat", "anomaly", "manual"}


def _validate_create(body: dict) -> str | None:
    title = (body.get("title") or "").strip()
    if not title or len(title) > 255:
        return "title required (1..255 chars)"
    body_md = (body.get("body_md") or "").strip()
    if not body_md:
        return "body_md required"
    if len(body_md) > 50_000:
        return "body_md too long (50000 char limit)"
    cluster_id = body.get("cluster_id")
    if cluster_id is not None and not _CLUSTER_ID_RE.match(str(cluster_id)):
        return "invalid cluster_id"
    source = body.get("source")
    if source is not None and source not in _VALID_SOURCES:
        return f"source must be one of {sorted(_VALID_SOURCES)}"
    tags = body.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or len(tags) > 16:
            return "tags must be an array of up to 16 strings"
        for t in tags:
            if not isinstance(t, str) or not (1 <= len(t) <= 64):
                return "each tag must be a 1..64 char string"
    return None


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _list_runbooks(qs: dict) -> dict:
    where = []
    params: dict = {}
    cluster_id = (qs.get("cluster_id") or "").strip()
    if cluster_id:
        where.append("cluster_id = :cid")
        params["cid"] = cluster_id
    tag = (qs.get("tag") or "").strip()
    if tag:
        where.append(":tag = ANY(tags)")
        params["tag"] = tag
    try:
        limit = max(1, min(int(qs.get("limit") or "50"), 200))
    except (TypeError, ValueError):
        limit = 50
    sql = (
        "SELECT id, cluster_id, title, summary_md, tags, source, source_ref, "
        "       created_by, created_at::text AS created_at "
        "FROM runbooks"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY created_at DESC LIMIT {limit}"
    rows = _execute(sql, params)
    return {"runbooks": rows, "count": len(rows)}


def _get_runbook(runbook_id: int) -> dict:
    rows = _execute(
        "SELECT id, cluster_id, title, summary_md, body_md, tags, source, "
        "       source_ref, created_by, created_at::text AS created_at, "
        "       updated_at::text AS updated_at "
        "FROM runbooks WHERE id = :id",
        {"id": runbook_id},
    )
    if not rows:
        return {}
    return rows[0]


def _create_runbook(body: dict, caller: str) -> dict:
    err = _validate_create(body)
    if err:
        return {"error": err, "_status": 400}
    tags = body.get("tags") or []
    # Postgres array literal: '{"a","b"}'
    tags_literal = (
        "{"
        + ",".join('"' + t.replace("\\", "\\\\").replace('"', '\\"') + '"' for t in tags)
        + "}"
    )
    rows = _execute(
        "INSERT INTO runbooks "
        "  (cluster_id, title, summary_md, body_md, tags, source, source_ref, created_by) "
        "VALUES "
        "  (:cluster_id, :title, :summary_md, :body_md, :tags::text[], "
        "   :source, :source_ref, :created_by) "
        "RETURNING id, cluster_id, title, summary_md, tags, source, source_ref, "
        "          created_by, created_at::text AS created_at",
        {
            "cluster_id": body.get("cluster_id"),
            "title": (body.get("title") or "").strip()[:255],
            "summary_md": (body.get("summary_md") or "").strip()[:1000] or None,
            "body_md": (body.get("body_md") or "").strip(),
            "tags": tags_literal,
            "source": body.get("source") or "manual",
            "source_ref": body.get("source_ref"),
            "created_by": caller,
        },
    )
    return rows[0] if rows else {}


def _delete_runbook(runbook_id: int) -> dict:
    rows = _execute(
        "DELETE FROM runbooks WHERE id = :id RETURNING id", {"id": runbook_id}
    )
    return {"deleted": rows[0]["id"] if rows else None}


# ---------------------------------------------------------------------------
# Lambda entry
# ---------------------------------------------------------------------------


def lambda_handler(event, context):
    method = (event.get("requestContext") or {}).get("http", {}).get("method") or event.get(
        "httpMethod", "GET"
    )
    qs = event.get("queryStringParameters") or {}
    path_params = event.get("pathParameters") or {}

    if method == "OPTIONS":
        return _resp(200, {})

    try:
        # Detail / delete routes
        runbook_id_str = path_params.get("id")
        if runbook_id_str:
            try:
                runbook_id = int(runbook_id_str)
            except (TypeError, ValueError):
                return _resp(400, {"error": "invalid id"})
            if method == "GET":
                rb = _get_runbook(runbook_id)
                if not rb:
                    return _resp(404, {"error": "not found"})
                return _resp(200, rb)
            if method == "DELETE":
                if not _is_admin(event):
                    return _resp(403, {"error": "admin role required"})
                return _resp(200, _delete_runbook(runbook_id))
            return _resp(405, {"error": f"method {method} not allowed"})

        # Collection routes
        if method == "GET":
            return _resp(200, _list_runbooks(qs))
        if method == "POST":
            # P2.4.2 server-side RBAC: creating runbooks is an admin action.
            if not _is_admin(event):
                return _resp(403, {"error": "admin role required"})
            try:
                body = json.loads(event.get("body") or "{}")
            except json.JSONDecodeError:
                return _resp(400, {"error": "invalid JSON body"})
            result = _create_runbook(body, _caller_name(event))
            if "_status" in result:
                status = result.pop("_status")
                return _resp(status, result)
            return _resp(201, {"runbook": result})
        return _resp(405, {"error": f"method {method} not allowed"})

    except Exception:
        print(f"Runbooks handler error: {traceback.format_exc()}")
        return _resp(500, {"error": "Internal server error"})
