"""WebSocket $connect REQUEST authorizer for the in-app alert push channel.

Browsers can't set custom headers on a WebSocket handshake, so the client
passes its Cognito ACCESS token as the `?token=` query param. We validate it by
calling Cognito GetUser(AccessToken) — a valid, unexpired token succeeds; an
invalid/expired one raises, and we deny. This avoids bundling a JWKS/crypto
library into the Lambda. Fail-closed: any error → Deny.
"""
import boto3
from botocore.exceptions import ClientError

_cognito = boto3.client("cognito-idp")


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
    token = params.get("token", "")
    if not token:
        # No token → deny (don't 500; an explicit Deny is the clean WS reject).
        return _policy("anonymous", "Deny", method_arn)
    try:
        user = _cognito.get_user(AccessToken=token)
        sub = ""
        for a in user.get("UserAttributes", []):
            if a.get("Name") == "sub":
                sub = a.get("Value", "")
                break
        return _policy(user.get("Username", "user"), "Allow", method_arn, sub)
    except ClientError as e:
        print(f"[ws-authorizer] token rejected: {e.response.get('Error', {}).get('Code')}")
        return _policy("anonymous", "Deny", method_arn)
    except Exception as e:
        print(f"[ws-authorizer] unexpected error: {type(e).__name__}")
        return _policy("anonymous", "Deny", method_arn)
