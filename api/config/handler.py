"""App-config API — DB-backed feature toggles an admin edits from the web UI.

Routes:
  GET /api/config   — list all known config keys (stored value or default)
  PUT /api/config   — upsert provided keys (admin-only)

Values are stored as strings in the dbops-{env}-app-config DynamoDB table
(PK config_key). A known-keys allowlist lives here so PUT can't write arbitrary
keys, and each key validates its own value. The API is decoupled from the
ticketing provider registry: TICKETING_PROVIDER validates FORMAT only — an
unwired provider name is inert at runtime (get_provider returns _UnwiredProvider).
"""

import base64
import json
import os
import re
import time

import boto3

# --- known-keys allowlist ---------------------------------------------------
# key -> (default, validator). validator(raw) returns the normalized string
# value to store, or raises ValueError with a human message.


def _v_ticketing_provider(raw) -> str:
    s = str(raw).strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", s):
        raise ValueError("TICKETING_PROVIDER must match [a-z0-9_-]{1,32}")
    return s


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def _v_bool(raw) -> str:
    if isinstance(raw, bool):
        return "true" if raw else "false"
    s = str(raw).strip().lower()
    if s in _TRUE:
        return "true"
    if s in _FALSE:
        return "false"
    raise ValueError("expected a boolean (true/false)")


CONFIG_KEYS: dict = {
    "TICKETING_PROVIDER": ("none", _v_ticketing_provider),
    "REPORT_DELIVERY_ENABLED": ("false", _v_bool),
}


# --- auth helpers (mirror api/saved_queries/handler.py) ---------------------


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
    return c.get("preferred_username") or c.get("cognito:username") or c.get("email") or "anonymous"


def _is_admin(event: dict) -> bool:
    # Fail-closed: a request without a parseable "Bearer <jwt>" is NOT admin.
    # The API Gateway JWT authorizer accepts a raw (scheme-less) token and
    # forwards it, so we must not treat unparseable auth as the dev-fallback
    # admin — only a VALID token with no group claim gets that fallback
    # (one-admin deploys), matching api/clusters/handler.py and the frontend.
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    # Empty claims == the token didn't decode (malformed/non-JWT after "Bearer ").
    # The gateway JWT authorizer rejects such tokens, but defense-in-depth: an
    # unparseable token is NOT the dev-fallback (which is a VALID token that
    # merely lacks a group claim — that always decodes to non-empty claims).
    if not claims:
        return False
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False
    if groups and "dbops-admin" not in groups:
        return False
    return True


# --- response + DDB helpers -------------------------------------------------


def _resp(status: int, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,PUT,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _table():
    return boto3.resource("dynamodb").Table(os.environ["APP_CONFIG_TABLE"])


def _read_all() -> dict:
    """Return {config_key: item} for every stored row (allowlisted keys only)."""
    out = {}
    table = _table()
    for key in CONFIG_KEYS:
        got = table.get_item(Key={"config_key": key}).get("Item")
        if got:
            out[key] = got
    return out


def _items_view(stored: dict) -> list:
    """Merge stored rows with defaults into the GET/PUT response shape."""
    items = []
    for key, (default, _validator) in CONFIG_KEYS.items():
        row = stored.get(key) or {}
        items.append({
            "key": key,
            "value": row.get("value", default),
            "default": default,
            "updated_at": row.get("updated_at"),
            "updated_by": row.get("updated_by"),
        })
    return items


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

    if method == "GET":
        return _resp(200, {"items": _items_view(_read_all())})

    if method == "PUT":
        try:
            body = json.loads(event.get("body") or "{}")
        except Exception:
            return _resp(400, {"error": "malformed JSON body"})
        config = body.get("config")
        if not isinstance(config, dict) or not config:
            return _resp(400, {"error": "body must be {\"config\": {KEY: value}}"})

        # validate everything BEFORE writing anything (no partial writes)
        normalized = {}
        for key, raw in config.items():
            if key not in CONFIG_KEYS:
                return _resp(400, {"error": f"unknown config key: {key}"})
            _default, validator = CONFIG_KEYS[key]
            try:
                normalized[key] = validator(raw)
            except ValueError as e:
                return _resp(400, {"error": f"{key}: {e}"})

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        who = _caller_name(event)
        table = _table()
        for key, value in normalized.items():
            table.put_item(Item={
                "config_key": key,
                "value": value,
                "updated_at": now,
                "updated_by": who,
            })
        return _resp(200, {"items": _items_view(_read_all())})

    return _resp(405, {"error": f"method {method} not allowed"})
