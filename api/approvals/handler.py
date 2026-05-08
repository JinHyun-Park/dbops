import json
import os
import uuid
from datetime import datetime
import boto3


def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ["APPROVALS_TABLE"])
    method = event.get("httpMethod", "GET")
    path_params = event.get("pathParameters") or {}
    approval_id = path_params.get("id")
    qsp = event.get("queryStringParameters") or {}

    headers = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}

    if method == "GET" and not approval_id:
        status_filter = qsp.get("status", "pending")
        response = table.scan(
            FilterExpression="approval_status = :s",
            ExpressionAttributeValues={":s": status_filter},
        )
        items = sorted(response.get("Items", []), key=lambda x: x.get("created_at", ""), reverse=True)
        return {"statusCode": 200, "headers": headers, "body": json.dumps(items, default=str)}

    if method == "GET" and approval_id:
        response = table.get_item(Key={"approval_id": approval_id, "created_at": qsp.get("created_at", "")})
        item = response.get("Item")
        if not item:
            response = table.scan(
                FilterExpression="approval_id = :aid",
                ExpressionAttributeValues={":aid": approval_id},
            )
            items = response.get("Items", [])
            item = items[0] if items else None
        return {
            "statusCode": 200 if item else 404,
            "headers": headers,
            "body": json.dumps(item or {"error": "not found"}, default=str),
        }

    if method == "POST":
        body = json.loads(event.get("body", "{}"))
        now = datetime.utcnow().isoformat()
        item = {
            "approval_id": str(uuid.uuid4()),
            "created_at": now,
            "cluster_id": body.get("cluster_id", ""),
            "tool_name": body.get("tool_name", ""),
            "action_description": body.get("action_description", ""),
            "parameters": json.dumps(body.get("parameters", {})),
            "risk_level": body.get("risk_level", "medium"),
            "requested_by": body.get("requested_by", "agent"),
            "approval_status": "pending",
        }
        table.put_item(Item=item)
        return {"statusCode": 201, "headers": headers, "body": json.dumps(item, default=str)}

    if method == "PUT" and approval_id:
        body = json.loads(event.get("body", "{}"))
        action = body.get("action")
        if action not in ("approve", "reject"):
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": "action must be approve or reject"})}

        response = table.scan(
            FilterExpression="approval_id = :aid",
            ExpressionAttributeValues={":aid": approval_id},
        )
        items = response.get("Items", [])
        if not items:
            return {"statusCode": 404, "headers": headers, "body": json.dumps({"error": "not found"})}

        item = items[0]
        table.update_item(
            Key={"approval_id": item["approval_id"], "created_at": item["created_at"]},
            UpdateExpression="SET approval_status = :s, resolved_at = :t, resolved_by = :by",
            ExpressionAttributeValues={
                ":s": "approved" if action == "approve" else "rejected",
                ":t": datetime.utcnow().isoformat(),
                ":by": body.get("approved_by", "dba"),
            },
        )
        return {
            "statusCode": 200, "headers": headers,
            "body": json.dumps({"approval_id": approval_id, "status": action + "d"}),
        }

    return {"statusCode": 405, "headers": headers, "body": json.dumps({"error": "Method not allowed"})}
