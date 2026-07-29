"""WebSocket $connect REQUEST authorizer for the in-app alert push channel.

Browsers can't set custom headers on a WebSocket handshake, so something has to
ride the query string. It used to be the Cognito ACCESS TOKEN, validated here via
Cognito GetUser. That worked, but it put a long-lived credential in a URL, which
was only acceptable while the WS stage had NO access logging: turning logs on would
have written the token to CloudWatch in plaintext. A constraint that has to be
remembered forever is one that will eventually be forgotten.

Now the client presents a TICKET minted by POST /api/ws-ticket (see
api/ws_ticket/handler.py): random, 60 seconds long, single-use, and it authorizes
nothing but one handshake. If it leaks there is almost nothing to take, so WS
access logging is no longer gated on this.

THREE THINGS THIS ENFORCES, and none of them is DynamoDB TTL:

 1. SINGLE USE. What enforces it is `delete_item(..., ReturnValues="ALL_OLD")`
    plus the rule below that an EMPTY old image is a Deny: DeleteItem is atomic per
    item, so of two concurrent handshakes bearing the same ticket exactly one
    receives the old image and the other receives nothing. Reading first and
    deleting after would be a race; this cannot be.

    The ConditionExpression is NOT what makes it single-use, and saying so would be
    an overclaim — DeleteItem on an absent key succeeds and simply returns no
    Attributes. The condition earns its place by turning "already gone" into an
    explicit ConditionalCheckFailed we can log, and by failing loudly if someone
    later refactors this to stop requiring the old image.
 2. EXPIRY, against this Lambda's own clock, using the `expires_at` the minting
    endpoint stored. DynamoDB TTL deletion is best-effort and can lag by up to 48
    hours, so relying on it would silently turn a 60-second ticket into a 48-hour
    one.
 3. FAIL-CLOSED. Missing ticket, unknown ticket, already-spent ticket, expired
    ticket, unconfigured table, or any unexpected error -> Deny. An explicit Deny is
    the clean WS reject; raising would surface as a 500.
"""
import os
import time

import boto3
from botocore.exceptions import ClientError

_ddb = boto3.resource("dynamodb")


def _policy(principal: str, effect: str, method_arn: str, sub: str = "") -> dict:
    return {
        "principalId": principal,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": method_arn,
                }
            ],
        },
        # Surfaced to the $connect handler as requestContext.authorizer.*
        "context": {"sub": sub},
    }


def lambda_handler(event, context):
    method_arn = event.get("methodArn", "*")
    params = event.get("queryStringParameters") or {}
    ticket = params.get("ticket", "")
    if not ticket:
        return _policy("anonymous", "Deny", method_arn)

    table_name = os.environ.get("WS_TICKETS_TABLE", "")
    if not table_name:
        print("[ws-authorizer] WS_TICKETS_TABLE not set - denying")
        return _policy("anonymous", "Deny", method_arn)

    try:
        # The delete IS the consume: atomic, so a replay loses the race by design.
        old = _ddb.Table(table_name).delete_item(
            Key={"ticket": ticket},
            ConditionExpression="attribute_exists(ticket)",
            ReturnValues="ALL_OLD",
        ).get("Attributes") or {}
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            # Unknown ticket, or one already spent by an earlier handshake.
            print("[ws-authorizer] ticket not present (unknown or already used)")
        else:
            print(f"[ws-authorizer] ticket lookup failed: {code}")
        return _policy("anonymous", "Deny", method_arn)
    except Exception as e:
        print(f"[ws-authorizer] unexpected error: {type(e).__name__}")
        return _policy("anonymous", "Deny", method_arn)

    try:
        expires_at = int(old.get("expires_at", 0))
    except (TypeError, ValueError):
        expires_at = 0
    if expires_at <= int(time.time()):
        # Consumed and refused: an expired ticket is spent either way, so a client
        # that sat on one has to mint a fresh one rather than retry with this.
        print("[ws-authorizer] ticket expired")
        return _policy("anonymous", "Deny", method_arn)

    sub = str(old.get("sub") or "")
    if not sub:
        # A row with no subject cannot identify anyone; do not invent one.
        print("[ws-authorizer] ticket carried no sub - denying")
        return _policy("anonymous", "Deny", method_arn)
    return _policy(sub, "Allow", method_arn, sub)
