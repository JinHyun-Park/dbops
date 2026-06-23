"""Context-files API — operator-uploaded reference text injected into the agent
prompt. Admin-only, fail-closed (mirrors api/config/handler.py). Text only;
per-file 32KB; 64KB total budget."""

import base64
import json
import os
import time
import uuid

import boto3

PER_FILE_MAX = 32768
TOTAL_MAX = 65536
ALLOWED_TYPES = {"md", "txt", "csv"}


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _claims(event: dict) -> dict:
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return {}
    return _decode_jwt_payload(auth.split(" ", 1)[1])


def _caller_name(event: dict) -> str:
    c = _claims(event)
    return c.get("preferred_username") or c.get("cognito:username") or c.get("email") or "unknown"


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
    if "dbops-viewer" in groups and "dbops-admin" not in groups:
        return False
    return True


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


def _table():
    return boto3.resource("dynamodb").Table(os.environ["CONTEXT_FILES_TABLE"])


def _scan_all(table) -> list:
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def _item_view(it: dict) -> dict:
    return {
        "file_id": it.get("file_id"),
        "name": it.get("name"),
        "content": it.get("content"),
        "content_type": it.get("content_type"),
        "size": int(it.get("size", 0)),
        "updated_at": it.get("updated_at"),
        "updated_by": it.get("updated_by"),
    }


def lambda_handler(event, context=None):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()

    if method == "OPTIONS":
        return _resp(200, {})
    if not _is_admin(event):
        return _resp(403, {"error": "admin only"})

    table = _table()
    file_id = (event.get("pathParameters") or {}).get("id")

    if method == "GET":
        return _resp(200, {"items": [_item_view(i) for i in _scan_all(table)]})

    if method == "POST":
        try:
            body = json.loads(event.get("body") or "{}")
        except Exception:
            return _resp(400, {"error": "malformed JSON body"})
        name = str(body.get("name") or "").strip()
        content = body.get("content")
        ctype = str(body.get("content_type") or "txt").strip().lower()
        if not name or len(name) > 128:
            return _resp(400, {"error": "name required (<=128 chars)"})
        if not isinstance(content, str) or not content:
            return _resp(400, {"error": "content must be a non-empty string"})
        if "\x00" in content:
            return _resp(400, {"error": "content must be text (binary not allowed)"})
        if ctype not in ALLOWED_TYPES:
            return _resp(400, {"error": f"content_type must be one of {sorted(ALLOWED_TYPES)}"})
        size = len(content.encode("utf-8"))
        if size > PER_FILE_MAX:
            return _resp(413, {"error": f"file too large ({size}B > {PER_FILE_MAX}B per-file cap)"})
        existing = _scan_all(table)
        used = sum(int(i.get("size", 0)) for i in existing)
        if used + size > TOTAL_MAX:
            return _resp(413, {"error": f"total context budget exceeded ({used}+{size} > {TOTAL_MAX}B)"})
        item = {
            "file_id": str(uuid.uuid4()), "name": name, "content": content,
            "content_type": ctype, "size": size,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updated_by": _caller_name(event),
        }
        table.put_item(Item=item)
        return _resp(201, _item_view(item))

    if method == "DELETE":
        if not file_id:
            return _resp(400, {"error": "file id required"})
        if "Item" not in table.get_item(Key={"file_id": file_id}):
            return _resp(404, {"error": "not found"})
        table.delete_item(Key={"file_id": file_id})
        return _resp(200, {"deleted": file_id})

    return _resp(405, {"error": f"method {method} not allowed"})
