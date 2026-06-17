"""WebSocket $disconnect — drop the connection row."""
import os

import boto3


def lambda_handler(event, context):
    connection_id = event.get("requestContext", {}).get("connectionId")
    if not connection_id:
        return {"statusCode": 400, "body": "missing connectionId"}
    table = boto3.resource("dynamodb").Table(os.environ["WS_CONNECTIONS_TABLE"])
    try:
        table.delete_item(Key={"connection_id": connection_id})
    except Exception as e:
        # Best-effort; a leftover row self-expires via TTL.
        print(f"[ws-disconnect] delete_item failed: {type(e).__name__}: {e}")
    return {"statusCode": 200, "body": "disconnected"}
