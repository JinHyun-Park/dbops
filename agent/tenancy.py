"""Agent-side cluster-visibility overlay. Mirrors api/clusters/tenancy.py logic
but resolves identity from the inbound request headers (AgentCore forwards the
Cognito Authorization header) instead of an API Gateway event. Returns the set
of cluster_ids the caller may see, or None for admins (no restriction)."""

import base64
import json
import logging
import os

import boto3
from boto3.dynamodb.conditions import Key

ADMIN_GROUP = "dbops-admin"

# AgentCore captures the `logging` logger into CloudWatch; bare print() does
# NOT surface reliably, so tenancy diagnostics/errors use this logger.
log = logging.getLogger("dbops.agent.tenancy")


def _decode_jwt_payload(token):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _claims_from_headers(headers):
    headers = headers or {}
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return {}
    return _decode_jwt_payload(auth.split(" ", 1)[1])


def _is_admin(claims):
    if not claims:
        return False
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False
    if groups and ADMIN_GROUP not in groups:
        return False
    return True


def _my_team_ids(username):
    if not username:
        return set()
    table_name = os.environ.get("TEAM_MEMBERS_TABLE", "")
    index = os.environ.get("TEAM_MEMBERS_BY_USER_INDEX", "by-user")
    if not table_name:
        return set()
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        resp = table.query(
            IndexName=index,
            KeyConditionExpression=Key("username").eq(username),
        )
        items = resp.get("Items", [])
        while resp.get("LastEvaluatedKey"):
            resp = table.query(
                IndexName=index,
                KeyConditionExpression=Key("username").eq(username),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
        teams = {it["team_id"] for it in items if it.get("team_id")}
        log.info(
            "[tenancy] my_team_ids user=…%s index=%s items=%d teams=%s",
            (username or "")[-6:], index, len(items), sorted(teams),
        )
        return teams
    except Exception as e:
        log.warning(
            "[tenancy] my_team_ids failed user=…%s index=%s table=%s: %s: %s",
            (username or "")[-6:], index, table_name, type(e).__name__, e,
        )
        return set()


def visible_cluster_ids_for(headers):
    """None => admin / all clusters (no restriction). Else the set of cluster_ids
    the caller may see (unassigned + their teams'). No/undecodable token => a
    non-admin with no teams (unassigned only). Registry-scan failure => None
    (fail-open to current behavior; never break chat on a transient DDB outage)."""
    claims = _claims_from_headers(headers)
    if _is_admin(claims):
        return None
    username = claims.get("cognito:username") or claims.get("sub") or ""
    teams = _my_team_ids(username)
    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not table_name:
        return None
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        resp = table.scan(ProjectionExpression="cluster_id, team_id")
        items = resp.get("Items", [])
        while resp.get("LastEvaluatedKey"):
            resp = table.scan(
                ProjectionExpression="cluster_id, team_id",
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
    except Exception as e:
        log.warning("[tenancy] registry scan failed: %s: %s", type(e).__name__, e)
        return None
    visible = set()
    for it in items:
        cid = it.get("cluster_id")
        if not cid:
            continue
        team = it.get("team_id")
        if not team or team in teams:
            visible.add(cid)
    log.info(
        "[tenancy] visible_cluster_ids_for user=…%s teams=%s scan=%d visible=%d",
        (username or "")[-6:], sorted(teams), len(items), len(visible),
    )
    return visible
