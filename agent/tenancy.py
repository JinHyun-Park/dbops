"""Agent-side cluster-visibility overlay.

Resolves the caller's identity from a JWKS-VERIFIED Cognito ID token the
frontend passes in a custom header (X-Amzn-Bedrock-AgentCore-Runtime-Custom-
Authorization). AgentCore's Cognito authorizer consumes the inbound
`Authorization` header and does NOT forward it to the container, so the agent
re-verifies a client-supplied copy of the ID token.

SECURITY: the custom-header token is client-supplied, so it is verified against
Cognito's JWKS (signature + issuer + audience + expiry + token_use=id) before any
claim is trusted. An unverifiable / forged / absent token yields no trusted
claims, which the visibility logic treats as a no-team viewer (unassigned
clusters only). A forged admin token can never grant access because it fails
signature verification — the only way to be recognized as admin or a team member
is a genuine, Cognito-signed token.

Returns the set of cluster_ids the caller may see, or None for admins
(no restriction).
"""

import logging
import os

import boto3
from boto3.dynamodb.conditions import Key

ADMIN_GROUP = "dbops-admin"
# AgentCore forwards request headers whose name starts with this prefix to the
# container (the bare Authorization header is consumed by the authorizer). The
# frontend sends the ID token as "<prefix>Authorization: Bearer <id_token>".
_CUSTOM_TOKEN_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-authorization"

log = logging.getLogger("dbops.agent.tenancy")

_jwks_client = None  # cached PyJWKClient — fetches Cognito JWKS once per container


def _cognito_issuer():
    region = os.environ.get("AWS_REGION_OVERRIDE") or os.environ.get("AWS_REGION", "")
    pool = os.environ.get("USER_POOL_ID", "")
    if not (region and pool):
        return "", ""
    issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool}"
    return issuer, f"{issuer}/.well-known/jwks.json"


def _verify_token(token):
    """Verify a Cognito ID token via JWKS. Returns the claims dict on success, or
    {} on ANY failure (bad signature, wrong issuer/audience, expired, not an ID
    token, or JWKS/infra error). {} => no trusted identity."""
    if not token:
        return {}
    issuer, jwks_url = _cognito_issuer()
    if not issuer:
        return {}
    client_id = os.environ.get("USER_POOL_CLIENT_ID", "")
    try:
        import jwt
        from jwt import PyJWKClient

        global _jwks_client
        if _jwks_client is None:
            _jwks_client = PyJWKClient(jwks_url)
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=issuer,
            audience=client_id or None,
            options={"verify_aud": bool(client_id)},
        )
        # Reject access tokens / anything that isn't a Cognito ID token.
        if claims.get("token_use") != "id":
            log.warning("[tenancy] token verify: unexpected token_use=%s", claims.get("token_use"))
            return {}
        return claims
    except Exception as e:
        log.warning("[tenancy] token verify failed: %s", type(e).__name__)
        return {}


def _claims_from_headers(headers):
    """Verified Cognito ID-token claims from the custom identity header, or {}."""
    headers = headers or {}
    raw = ""
    for k, v in headers.items():
        if isinstance(k, str) and k.lower() == _CUSTOM_TOKEN_HEADER:
            raw = v or ""
            break
    if not raw.lower().startswith("bearer "):
        return {}
    return _verify_token(raw.split(" ", 1)[1])


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
        return {it["team_id"] for it in items if it.get("team_id")}
    except Exception as e:
        log.warning("[tenancy] my_team_ids failed: %s: %s", type(e).__name__, e)
        return set()


def visible_cluster_ids_for(headers):
    """None => admin / all clusters (no restriction). Else the set of cluster_ids
    the caller may see (unassigned + their teams'). Unverified / absent token =>
    a non-admin with no teams (unassigned clusters only — the default-open
    baseline; a forged token cannot escalate because verification fails).
    Registry-scan failure => None (fail-open; never break chat on a DDB outage)."""
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
    return visible
