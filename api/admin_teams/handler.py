"""Admin Teams & cluster-assignment management API (admin-gated).

Routes:
  GET    /api/admin/teams                                  — list teams
  POST   /api/admin/teams                                  — create {name}
  GET    /api/admin/teams/{team_id}                        — detail (members+clusters)
  DELETE /api/admin/teams/{team_id}                        — delete (unassigns clusters)
  POST   /api/admin/teams/{team_id}/members/{username}     — add member
  DELETE /api/admin/teams/{team_id}/members/{username}     — remove member
  POST   /api/admin/teams/{team_id}/clusters/{cluster_id}  — assign cluster
  DELETE /api/admin/teams/{team_id}/clusters/{cluster_id}  — unassign cluster

Teams gate cluster VISIBILITY (see api/*/tenancy.py); they do not change role.
Admin-gated, fail-closed (mirror api/admin_users/handler.py)."""

import base64
import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

ADMIN_GROUP = "dbops-admin"


def _decode_jwt_payload(token):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _is_admin(event):
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
    if groups and ADMIN_GROUP not in groups:
        return False
    return True


def _caller_username(event):
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return ""
    c = _decode_jwt_payload(auth.split(" ", 1)[1])
    return c.get("cognito:username") or c.get("sub") or ""


def _resp(status, body):
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


def _teams_table():
    return boto3.resource("dynamodb").Table(os.environ["TEAMS_TABLE"])


def _members_table():
    return boto3.resource("dynamodb").Table(os.environ["TEAM_MEMBERS_TABLE"])


def _clusters_table():
    return boto3.resource("dynamodb").Table(os.environ["CLUSTERS_TABLE"])


def _scan_all(table, **kwargs):
    items = []
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def _query_all(table, **kwargs):
    items = []
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def _list_teams():
    teams = _scan_all(_teams_table())
    members_tbl = _members_table()
    out = []
    for t in teams:
        tid = t.get("team_id")
        mcount = len(_query_all(members_tbl, KeyConditionExpression=Key("team_id").eq(tid)))
        out.append({
            "team_id": tid,
            "name": t.get("name"),
            "created_at": t.get("created_at"),
            "created_by": t.get("created_by"),
            "member_count": mcount,
        })
    return out


def _team_detail(tid):
    t = _teams_table().get_item(Key={"team_id": tid}).get("Item")
    if not t:
        return None
    member_rows = _query_all(_members_table(), KeyConditionExpression=Key("team_id").eq(tid))
    members = [m.get("username") for m in member_rows]
    clusters = [c.get("cluster_id") for c in _scan_all(
        _clusters_table(),
        FilterExpression="team_id = :tid",
        ExpressionAttributeValues={":tid": tid},
    )]
    return {
        "team_id": tid,
        "name": t.get("name"),
        "members": members,
        "clusters": clusters,
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

    pp = event.get("pathParameters") or {}
    tid = pp.get("team_id")
    username = pp.get("username")
    cluster_id = pp.get("cluster_id")

    try:
        # ----- member sub-routes -----
        if username and tid:
            if method == "POST":
                if not _teams_table().get_item(Key={"team_id": tid}).get("Item"):
                    return _resp(404, {"error": "team not found"})
                _members_table().put_item(Item={"team_id": tid, "username": username})
                return _resp(200, {"team_id": tid, "username": username, "member": True})
            if method == "DELETE":
                _members_table().delete_item(Key={"team_id": tid, "username": username})
                return _resp(200, {"team_id": tid, "username": username, "member": False})
            return _resp(405, {"error": "method not allowed"})

        # ----- cluster assignment sub-routes -----
        if cluster_id and tid:
            if method == "POST":
                if not _teams_table().get_item(Key={"team_id": tid}).get("Item"):
                    return _resp(404, {"error": "team not found"})
                _clusters_table().update_item(
                    Key={"cluster_id": cluster_id},
                    UpdateExpression="SET team_id = :t",
                    ExpressionAttributeValues={":t": tid},
                )
                return _resp(200, {"cluster_id": cluster_id, "team_id": tid})
            if method == "DELETE":
                _clusters_table().update_item(
                    Key={"cluster_id": cluster_id},
                    UpdateExpression="REMOVE team_id",
                )
                return _resp(200, {"cluster_id": cluster_id, "team_id": None})
            return _resp(405, {"error": "method not allowed"})

        # ----- team-level routes -----
        if tid:
            if method == "GET":
                d = _team_detail(tid)
                return _resp(200, d) if d else _resp(404, {"error": "team not found"})
            if method == "DELETE":
                # Unassign every cluster pointing at this team (paginated)
                for c in _scan_all(
                    _clusters_table(),
                    FilterExpression="team_id = :t",
                    ExpressionAttributeValues={":t": tid},
                ):
                    _clusters_table().update_item(
                        Key={"cluster_id": c["cluster_id"]},
                        UpdateExpression="REMOVE team_id",
                    )
                # Delete all member rows (paginated)
                for m in _query_all(_members_table(), KeyConditionExpression=Key("team_id").eq(tid)):
                    _members_table().delete_item(Key={"team_id": tid, "username": m["username"]})
                # Delete the team row
                _teams_table().delete_item(Key={"team_id": tid})
                return _resp(200, {"team_id": tid, "deleted": True})
            return _resp(405, {"error": "method not allowed"})

        # ----- collection routes -----
        if method == "GET":
            return _resp(200, {"teams": _list_teams()})
        if method == "POST":
            try:
                body = json.loads(event.get("body") or "{}")
            except Exception:
                return _resp(400, {"error": "malformed JSON body"})
            name = (body.get("name") or "").strip()
            if not name:
                return _resp(400, {"error": "name required"})
            new_tid = "team-" + uuid.uuid4().hex[:12]
            _teams_table().put_item(Item={
                "team_id": new_tid,
                "name": name,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "created_by": _caller_username(event),
            })
            return _resp(201, {"team_id": new_tid, "name": name})
        return _resp(405, {"error": "method not allowed"})

    except ClientError as e:
        print(f"[admin_teams] DynamoDB error: {e}")
        return _resp(500, {"error": "operation failed"})
