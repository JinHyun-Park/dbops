"""WebSocket $connect — record the connection so broadcasters can reach it.

Auth already happened in the REQUEST authorizer (Cognito access token); here we
just persist the connectionId (+ the authorizer's sub) with a TTL safety net so
abandoned rows self-clean even if $disconnect is missed.
"""
import os
import time

import boto3

_TTL_SECONDS = 2 * 60 * 60  # 2h backstop; WS idle-timeout (10m) usually wins.


def lambda_handler(event, context):
    ctx = event.get("requestContext", {})
    connection_id = ctx.get("connectionId")
    if not connection_id:
        return {"statusCode": 400, "body": "missing connectionId"}
    sub = (ctx.get("authorizer") or {}).get("sub", "")
    table = boto3.resource("dynamodb").Table(os.environ["WS_CONNECTIONS_TABLE"])
    try:
        table.put_item(
            Item={
                "connection_id": connection_id,
                "user_sub": sub,
                "connected_at": int(time.time()),
                "ttl": int(time.time()) + _TTL_SECONDS,
            }
        )
    except Exception as e:
        print(f"[ws-connect] put_item failed: {type(e).__name__}: {e}")
        return {"statusCode": 500, "body": "connect failed"}
    return {"statusCode": 200, "body": "connected"}
