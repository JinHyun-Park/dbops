"""Chat sessions API — DynamoDB-backed conversation persistence.

The chat UI used to keep conversations only in browser localStorage.
That works fine on one device but breaks the moment the DBA opens the
console from a different laptop or clears the browser cache. This
handler is the cross-device source of truth: conversation rows live in
the `sessions` DDB table, keyed by user (Cognito sub or username).

Table layout (table already exists in foundation_stack with TTL):
  PK   session_id (string) — UUID prefixed with `dbops-session-`
  GSI  user-updated-index  — partition user_id, sort updated_at desc
  attrs: title, cluster_id, message_count, messages (list), ttl

Messages are embedded directly in the row. DDB items max out at 400KB —
that's roughly 4000 plain-text messages or 1000 messages with tool calls.
A future migration to a separate `chat_messages` table can lift this
ceiling when we actually hit it.

Routes (registered as proxy in agent_stack):
  GET    /api/chat/sessions                    — list caller's sessions
  GET    /api/chat/sessions/{id}               — fetch one with full messages
  PUT    /api/chat/sessions/{id}               — upsert whole session
  DELETE /api/chat/sessions/{id}               — delete one
"""

from __future__ import annotations

import base64
import json
import os
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

# 90 days of retention by default — enough to recover prior week's
# incident transcripts but not so long that orphan sessions accumulate.
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", str(90 * 24 * 3600)))

# Soft cap on messages embedded in a single DDB item. Beyond this the
# write truncates the oldest messages so the 400KB DDB limit isn't hit.
MAX_EMBEDDED_MESSAGES = int(os.environ.get("MAX_EMBEDDED_MESSAGES", "400"))


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _caller_id(event: dict) -> str:
    """Cognito `sub` is the stable identifier across username changes —
    prefer it over username/email which can be remapped."""
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return ""
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    return (
        claims.get("sub")
        or claims.get("cognito:username")
        or claims.get("email")
        or ""
    )


def _table():
    return boto3.resource("dynamodb").Table(os.environ["SESSIONS_TABLE"])


def _response(status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=_json_default),
    }


def _json_default(o):
    """DDB returns Decimal for numbers — convert to plain int/float for JSON."""
    if isinstance(o, Decimal):
        return float(o) if o % 1 else int(o)
    return str(o)


def _normalize_messages(messages: list) -> list:
    """Defensive: only keep the fields we expect. Frontend may evolve
    its message shape, and we don't want to store arbitrary blobs."""
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        entry: dict = {
            "role": str(m.get("role", "user"))[:24],
            "content": str(m.get("content", ""))[:50_000],
            "tool_calls": m.get("toolCalls") or m.get("tool_calls") or [],
            "ts": int(m.get("ts") or time.time() * 1000),
        }
        # Follow-up suggestions: list of short strings generated post-response.
        # Cap count and length so a runaway frontend can't blow the DDB limit.
        raw_followups = m.get("followups")
        if raw_followups and isinstance(raw_followups, list):
            entry["followups"] = [x[:300] for x in raw_followups if isinstance(x, str)][:5]
        # Incomplete flag: set when a stream was interrupted before onDone.
        if m.get("incomplete"):
            entry["incomplete"] = True
        out.append(entry)
    # Embed only the most recent N messages so we never blow the DDB row limit.
    if len(out) > MAX_EMBEDDED_MESSAGES:
        out = out[-MAX_EMBEDDED_MESSAGES:]
    return out


def lambda_handler(event, context):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    )
    path = event.get("rawPath") or event.get("path") or ""
    path_params = event.get("pathParameters") or {}
    session_id = path_params.get("id") or path_params.get("sessionId")
    qsp = event.get("queryStringParameters") or {}

    user_id = _caller_id(event)
    if not user_id:
        return _response(401, {"error": "unauthenticated"})

    try:
        table = _table()
    except KeyError:
        return _response(500, {"error": "SESSIONS_TABLE not configured"})

    if method == "GET" and not session_id:
        return _list_sessions(table, user_id, qsp)

    if method == "GET" and session_id:
        return _get_session(table, user_id, session_id)

    if method == "PUT" and session_id:
        return _put_session(table, user_id, session_id, event)

    if method == "DELETE" and session_id:
        return _delete_session(table, user_id, session_id)

    return _response(405, {"error": f"method {method} not allowed for {path}"})


def _list_sessions(table, user_id: str, qsp: dict) -> dict:
    limit = int(qsp.get("limit", "50"))
    limit = max(1, min(limit, 200))
    try:
        resp = table.query(
            IndexName="user-updated-index",
            KeyConditionExpression=Key("user_id").eq(user_id),
            ScanIndexForward=False,  # most recent first
            Limit=limit,
            # Don't pull `messages` blob in list view — keeps payload small.
            ProjectionExpression="session_id, title, cluster_id, updated_at, message_count, created_at, total_input_tokens, total_output_tokens, last_error",
        )
    except ClientError as e:
        print(f"[chat_sessions] list query failed for {user_id}: {e}")
        return _response(500, {"error": "대화 목록을 불러오지 못했습니다. 잠시 후 다시 시도하세요."})
    return _response(200, {"sessions": resp.get("Items", [])})


def _get_session(table, user_id: str, session_id: str) -> dict:
    try:
        resp = table.get_item(Key={"session_id": session_id})
    except ClientError as e:
        print(f"[chat_sessions] get_item failed for {session_id}: {e}")
        return _response(500, {"error": "대화를 불러오지 못했습니다. 잠시 후 다시 시도하세요."})
    item = resp.get("Item")
    if not item:
        return _response(404, {"error": "session not found"})
    if item.get("user_id") != user_id:
        # Don't leak existence of someone else's session.
        return _response(404, {"error": "session not found"})
    return _response(200, item)


def _put_session(table, user_id: str, session_id: str, event: dict) -> dict:
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "body must be valid JSON"})

    title = str(body.get("title", "") or "Untitled")[:200]
    cluster_id = str(body.get("cluster_id", "") or "")[:255]
    messages = _normalize_messages(body.get("messages") or [])
    now_ms = int(time.time() * 1000)

    # Authorization: if the row exists and belongs to someone else, refuse.
    try:
        existing = table.get_item(Key={"session_id": session_id}).get("Item") or {}
    except ClientError as e:
        print(f"[chat_sessions] ownership read failed for {session_id}: {e}")
        return _response(500, {"error": "대화 소유자 확인에 실패했습니다. 잠시 후 다시 시도하세요."})
    if existing and existing.get("user_id") != user_id:
        return _response(403, {"error": "not your session"})

    item = {
        "session_id": session_id,
        "user_id": user_id,
        "title": title,
        "cluster_id": cluster_id,
        "messages": messages,
        "message_count": len(messages),
        "updated_at": now_ms,
        "created_at": existing.get("created_at", now_ms),
        "ttl": int(time.time()) + SESSION_TTL_SECONDS,
    }
    for k in ("total_input_tokens", "total_output_tokens", "turn_count"):
        v = body.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            item[k] = int(v)
    if isinstance(body.get("last_error"), dict):
        item["last_error"] = body["last_error"]
    try:
        table.put_item(Item=item)
    except ClientError as e:
        print(f"[chat_sessions] put_item failed for {session_id}: {e}")
        return _response(500, {"error": "대화 저장에 실패했습니다. 잠시 후 다시 시도하세요."})
    return _response(200, item)


def _delete_session(table, user_id: str, session_id: str) -> dict:
    try:
        existing = table.get_item(Key={"session_id": session_id}).get("Item") or {}
    except ClientError as e:
        print(f"[chat_sessions] pre-delete read failed for {session_id}: {e}")
        return _response(500, {"error": "삭제할 대화를 조회하지 못했습니다. 잠시 후 다시 시도하세요."})
    if not existing:
        return _response(404, {"error": "session not found"})
    if existing.get("user_id") != user_id:
        return _response(403, {"error": "not your session"})
    try:
        table.delete_item(Key={"session_id": session_id})
    except ClientError as e:
        print(f"[chat_sessions] delete_item failed for {session_id}: {e}")
        return _response(500, {"error": "대화 삭제에 실패했습니다. 잠시 후 다시 시도하세요."})
    return _response(200, {"deleted": session_id})
