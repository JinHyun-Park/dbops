"""Best-effort WebSocket broadcast of an alert/incident to connected clients.

Pushes the payload to every connection in the WS connections table via the API
Gateway Management API. Prunes stale (Gone) connections, never raises into the
caller, and is a no-op when the WS push channel isn't configured (env unset) —
so callers can broadcast unconditionally without guarding.

Copied verbatim into each broadcasting Lambda's package (alert_evaluator,
incident_webhook) to avoid a shared layer; keep the copies in sync.
"""
import json
import os

import boto3


def broadcast(payload: dict) -> int:
    """Push `payload` to all connected WS clients; return the delivered count."""
    table_name = os.environ.get("WS_CONNECTIONS_TABLE")
    endpoint = os.environ.get("WS_MGMT_ENDPOINT")
    if not table_name or not endpoint:
        return 0  # push channel not configured on this deployment
    ddb = boto3.resource("dynamodb").Table(table_name)
    mgmt = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)
    data = json.dumps(payload, default=str).encode("utf-8")

    items = []
    scan_kwargs = {"ProjectionExpression": "connection_id"}
    try:
        while True:
            resp = ddb.scan(**scan_kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as e:
        print(f"[ws-notify] connections scan failed: {type(e).__name__}: {e}")
        return 0

    delivered = 0
    for it in items:
        cid = it.get("connection_id")
        if not cid:
            continue
        try:
            mgmt.post_to_connection(ConnectionId=cid, Data=data)
            delivered += 1
        except mgmt.exceptions.GoneException:
            try:
                ddb.delete_item(Key={"connection_id": cid})
            except Exception:
                pass
        except Exception as e:
            print(f"[ws-notify] post_to_connection {cid[:8]} failed: {type(e).__name__}")
    return delivered
