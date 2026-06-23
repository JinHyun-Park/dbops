"""Approval-policies API — admin-defined designated-approver routing.

Routes:
  GET    /api/approval-policies        — list all policies
  POST   /api/approval-policies        — create (generates policy_id)
  PUT    /api/approval-policies/{id}    — update
  DELETE /api/approval-policies/{id}    — delete

A policy = {policy_id, cluster_id, action_type, approvers[], description,
updated_at, updated_by}. cluster_id / action_type are an exact value or "*".
approvers are emails/usernames, stored trimmed + lower-cased. Admin-only and
fail-closed (same gate as api/config/handler.py).
"""

import base64
import json
import os
import time
import uuid

import boto3


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
    # Fail-closed (mirrors api/config/handler.py): no parseable "Bearer <jwt>"
    # or empty claims is NOT admin; only a valid token without a viewer-only
    # group claim is admin (one-admin dev fallback).
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


def _table():
    return boto3.resource("dynamodb").Table(os.environ["APPROVAL_POLICIES_TABLE"])


def _scan_all(table) -> list:
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def _validate(body) -> tuple:
    """Return (policy_fields, error). policy_fields excludes policy_id/updated_*."""
    if not isinstance(body, dict):
        return None, "body must be a JSON object"
    cluster_id = str(body.get("cluster_id") or "*").strip() or "*"
    action_type = str(body.get("action_type") or "*").strip() or "*"
    raw_approvers = body.get("approvers")
    if not isinstance(raw_approvers, list):
        return None, "approvers must be a list"
    approvers = sorted({str(a).strip().lower() for a in raw_approvers if str(a).strip()})
    if not approvers:
        return None, "approvers must contain at least one non-empty entry"
    description = str(body.get("description") or "").strip()
    return {
        "cluster_id": cluster_id,
        "action_type": action_type,
        "approvers": approvers,
        "description": description,
    }, None


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
    policy_id = (event.get("pathParameters") or {}).get("id")

    if method == "GET":
        return _resp(200, {"policies": _scan_all(table)})

    if method in ("POST", "PUT"):
        try:
            body = json.loads(event.get("body") or "{}")
        except Exception:
            return _resp(400, {"error": "malformed JSON body"})
        fields, err = _validate(body)
        if err:
            return _resp(400, {"error": err})
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        who = _caller_name(event)
        if method == "PUT":
            if not policy_id:
                return _resp(400, {"error": "policy id required"})
            if "Item" not in table.get_item(Key={"policy_id": policy_id}):
                return _resp(404, {"error": "policy not found"})
        else:
            policy_id = str(uuid.uuid4())
        item = {"policy_id": policy_id, **fields, "updated_at": now, "updated_by": who}
        table.put_item(Item=item)
        return _resp(200 if method == "PUT" else 201, item)

    if method == "DELETE":
        if not policy_id:
            return _resp(400, {"error": "policy id required"})
        if "Item" not in table.get_item(Key={"policy_id": policy_id}):
            return _resp(404, {"error": "policy not found"})
        table.delete_item(Key={"policy_id": policy_id})
        return _resp(200, {"deleted": policy_id})

    return _resp(405, {"error": f"method {method} not allowed"})
