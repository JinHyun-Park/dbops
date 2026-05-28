"""Agent memory inspection API.

Surfaces the user's AgentCore Memory records so the DBA can see what
the agent has remembered about them — and delete entries that became
incorrect or were captured under a wrong assumption.

Three namespaces are wired in the agent stack:
  /users/{actorId}/facts        — semantic facts
  /users/{actorId}/preferences  — user preferences (default tone, etc.)
  /summaries/{actorId}/{sessionId} — per-session summaries

This handler exposes the first two by `kind=facts|preferences`. Session
summaries are intentionally not exposed — they're internal scaffolding
the agent uses to keep context across turns, and editing them would
break in-flight conversations.

Routes:
  GET    /api/memory                      — list records (?kind=)
  DELETE /api/memory/{record_id}?kind=…   — delete one record
"""

from __future__ import annotations

import base64
import json
import os

import boto3
from botocore.exceptions import ClientError

_KIND_TO_NAMESPACE = {
    "facts": "/users/{actor}/facts",
    "preferences": "/users/{actor}/preferences",
}


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
    """AgentCore Memory uses Cognito sub as actorId via the system prompt
    template `{actorId}`. Returning the sub directly so the namespace
    resolution matches whatever the agent writes to."""
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return ""
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    return claims.get("sub") or claims.get("cognito:username") or ""


def _resp(status: int, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str),
    }


def _agentcore():
    return boto3.client("bedrock-agentcore")


def lambda_handler(event, context):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    )
    path_params = event.get("pathParameters") or {}
    qsp = event.get("queryStringParameters") or {}
    record_id = path_params.get("id") or path_params.get("recordId")
    kind = qsp.get("kind", "preferences")

    if kind not in _KIND_TO_NAMESPACE:
        return _resp(400, {"error": f"kind must be one of {sorted(_KIND_TO_NAMESPACE)}"})

    actor = _caller_id(event)
    if not actor:
        return _resp(401, {"error": "unauthenticated"})

    memory_id = os.environ.get("MEMORY_ID", "")
    if not memory_id:
        return _resp(500, {"error": "MEMORY_ID not configured"})

    namespace = _KIND_TO_NAMESPACE[kind].format(actor=actor)
    agentcore = _agentcore()

    if method == "GET" and not record_id:
        return _list_records(agentcore, memory_id, namespace, kind)

    if method == "DELETE" and record_id:
        return _delete_record(agentcore, memory_id, namespace, record_id, kind)

    return _resp(405, {"error": f"method {method} not allowed"})


def _list_records(agentcore, memory_id: str, namespace: str, kind: str) -> dict:
    try:
        resp = agentcore.list_memory_records(
            memoryId=memory_id,
            namespace=namespace,
            maxResults=50,
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        # ResourceNotFoundException means the actor has no records yet —
        # that's fine, just return an empty list rather than 500ing.
        if code in ("ResourceNotFoundException", "ValidationException"):
            return _resp(200, {"namespace": namespace, "kind": kind, "records": []})
        return _resp(500, {"error": f"{code}: {str(e)[:200]}"})

    records = []
    for r in resp.get("memoryRecordSummaries", []) or resp.get("records", []) or []:
        # The API shape is `memoryRecordSummaries` per current SDK; keep
        # the alternate `records` for older revs.
        records.append({
            "id": r.get("memoryRecordId") or r.get("recordId"),
            "content": (r.get("content") or {}).get("text")
            if isinstance(r.get("content"), dict)
            else r.get("content"),
            "created_at": r.get("createdAt"),
            "updated_at": r.get("updatedAt"),
        })
    return _resp(200, {"namespace": namespace, "kind": kind, "records": records})


def _delete_record(
    agentcore, memory_id: str, namespace: str, record_id: str, kind: str
) -> dict:
    try:
        agentcore.delete_memory_record(
            memoryId=memory_id,
            memoryRecordId=record_id,
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            return _resp(404, {"error": "record not found"})
        return _resp(500, {"error": f"{code}: {str(e)[:200]}"})
    return _resp(200, {"deleted": record_id, "kind": kind})
