"""Multi-team cluster-visibility overlay.

VENDORED MODULE — keep byte-identical across all api/*/tenancy.py copies
(tests/unit/api/test_tenancy_parity.py enforces this). api/ Lambdas are
independent packages and cannot share imports, so the overlay is copied, like
engine_family.py.

Default-open: a cluster with no team_id is visible to everyone. A cluster with
a team_id is visible only to members of that team + admins. Admins see all.
On infra error my_team_ids() returns an empty set, which keeps unassigned
clusters visible (fail-open) while hiding assigned clusters (fail-closed).
"""

import base64
import json
import os

import boto3
from boto3.dynamodb.conditions import Key

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


def _claims(event):
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return {}
    return _decode_jwt_payload(auth.split(" ", 1)[1])


def is_admin(event):
    """Mirror api/clusters/handler.py::_is_admin — admin if dbops-admin in
    groups OR no groups at all; fail-closed on missing/invalid bearer."""
    claims = _claims(event)
    if not claims:
        return False
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False
    if groups and ADMIN_GROUP not in groups:
        return False
    return True


def caller_username(event):
    c = _claims(event)
    return c.get("cognito:username") or c.get("sub") or ""


def my_team_ids(username):
    """Team ids the user belongs to, via the team_members by-user GSI. Empty
    set on no-username / no-table / infra error (caller treats empty as
    'unassigned clusters only')."""
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
        return {it["team_id"] for it in items if it.get("team_id")}
    except Exception as e:
        print(f"[tenancy] my_team_ids failed for {username}: {e}")
        return set()


def visible_cluster_ids(event, cluster_items):
    """None => all clusters (admin). Else the set of cluster_ids the caller may
    see, given already-fetched registry items (each a dict with 'cluster_id'
    and optional 'team_id')."""
    if is_admin(event):
        return None
    teams = my_team_ids(caller_username(event))
    visible = set()
    for it in (cluster_items or []):
        cid = it.get("cluster_id")
        if not cid:
            continue
        team = it.get("team_id")
        if not team:
            visible.add(cid)          # unassigned => default-open
        elif team in teams:
            visible.add(cid)          # assigned to a team I'm in
    return visible


def visible_set_from_registry(event):
    """Convenience for LIST handlers that don't already hold the cluster
    registry: scan CLUSTERS_TABLE for {cluster_id, team_id} and return the
    visible cluster_id set. None for admins (no filter). On a registry-scan
    failure returns None (fail-open to current behavior — consistent with the
    fleet filter; a transient DDB outage must not blank a list)."""
    if is_admin(event):
        return None
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
        print(f"[tenancy] visible_set_from_registry scan failed: {e}")
        return None
    return visible_cluster_ids(event, items)


def cluster_visible(event, cluster_item):
    """Single-cluster visibility for per-cluster routes. Admin => True.
    Unassigned (or missing registry item) => True (default-open). Assigned =>
    True iff the caller is a member of the cluster's team."""
    if is_admin(event):
        return True
    team = (cluster_item or {}).get("team_id")
    if not team:
        return True
    return team in my_team_ids(caller_username(event))
