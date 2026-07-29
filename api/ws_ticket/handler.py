"""POST /api/ws-ticket — mint a short-lived, single-use ticket for the WS handshake.

WHY THIS EXISTS
---------------
Browsers cannot set headers on a WebSocket handshake, so something has to ride the
query string. That used to be the Cognito ACCESS TOKEN itself, which meant a
long-lived credential sat in a URL: acceptable only while the WS stage had NO
access logging, since enabling it would have written the token to CloudWatch in
plaintext, and it is the sort of constraint that gets forgotten.

This endpoint mints a value that is worth almost nothing if it leaks: random,
60 seconds long, single-use, and it authorizes NOTHING except one WS handshake.
The real credential stays in the Authorization header of this ordinary REST call,
which API Gateway's JWT authorizer has already validated before this code runs.

THE TTL IS NOT THE EXPIRY CHECK
------------------------------
The row carries a DynamoDB `ttl` attribute so used and abandoned tickets get
reaped, but DynamoDB TTL deletion is best-effort and can lag by up to 48 hours.
Treating it as the expiry would leave a 48-hour ticket. `expires_at` is stored
explicitly and the AUTHORIZER compares it against its own clock; the TTL is only
housekeeping. Single-use is enforced there too, by a conditional delete.

Fail-closed: no identity, no table, or a write failure returns an error and no
ticket, so the client falls back to its existing polling path rather than
connecting unauthenticated.
"""

import base64
import json
import os
import secrets
import time

import boto3

# 60s is generous for "fetch a ticket, then open a socket" and short enough that a
# leaked ticket is worthless before it can be replayed by hand.
TICKET_TTL_SECONDS = 60
# Reaping happens well after the ticket is useless; see the module docstring on why
# this is housekeeping and not the expiry check.
_REAP_AFTER_SECONDS = 300


def _decode_jwt_payload(token):
    """Claims from a bearer token WITHOUT verifying the signature.

    Safe here and only here: API Gateway's JWT authorizer has already verified
    this request's token against the Cognito JWKS before the Lambda is invoked, so
    an unverified read of the payload cannot be reached with a forged token. Same
    reasoning as the other api/ handlers that read claims this way.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _caller(event):
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return ""
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    return claims.get("sub") or claims.get("cognito:username") or ""


def _response(status, body, origin="*"):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": origin,
            # A ticket must never be cached by anything: it is single-use and its
            # whole value is that it is fresh.
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body),
    }


def lambda_handler(event, context):
    origin = (event.get("headers") or {}).get("origin") or "*"
    method = ((event.get("requestContext") or {}).get("http") or {}).get(
        "method"
    ) or event.get("httpMethod", "POST")
    if method == "OPTIONS":
        return _response(200, {}, origin)

    table_name = os.environ.get("WS_TICKETS_TABLE", "")
    if not table_name:
        # Push channel not configured on this deployment. Not an error the operator
        # needs to see: the client keeps polling.
        return _response(503, {"error": "ws ticket channel is not configured"}, origin)

    sub = _caller(event)
    if not sub:
        # The JWT authorizer should have rejected this already; refuse rather than
        # mint a ticket bound to nobody.
        return _response(401, {"error": "unauthenticated"}, origin)

    now = int(time.time())
    ticket = secrets.token_urlsafe(32)
    try:
        boto3.resource("dynamodb").Table(table_name).put_item(
            Item={
                "ticket": ticket,
                "sub": sub,
                # Checked by the authorizer against its own clock.
                "expires_at": now + TICKET_TTL_SECONDS,
                # Housekeeping only.
                "ttl": now + _REAP_AFTER_SECONDS,
            }
        )
    except Exception:
        # No detail in the payload: a DDB error can carry the table ARN and the
        # account id. The client degrades to polling.
        print(f"[ws-ticket] put_item failed for sub={sub[:8]}...")
        return _response(500, {"error": "could not mint a ticket"}, origin)

    return _response(200, {"ticket": ticket, "expires_in": TICKET_TTL_SECONDS}, origin)
