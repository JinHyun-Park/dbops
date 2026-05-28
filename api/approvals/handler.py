import json
import os
import uuid
from datetime import datetime

import boto3


def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ["APPROVALS_TABLE"])
    method = event.get("requestContext", {}).get("http", {}).get("method", event.get("httpMethod", "GET"))
    path = event.get("rawPath") or event.get("path") or ""
    path_params = event.get("pathParameters") or {}
    approval_id = path_params.get("id")
    qsp = event.get("queryStringParameters") or {}

    headers = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}

    # /api/activity — chronological feed of every approval (any status)
    # for compliance + retro queries ("what writes happened in cluster X
    # last week?"). The DDB scan is cheap because approvals are short-
    # lived: rows expire on TTL or get consumed by the next write.
    if method == "GET" and path.endswith("/activity"):
        cluster_filter = qsp.get("cluster_id")
        actor_filter = qsp.get("actor")
        action_filter = qsp.get("action_type")
        limit = max(1, min(int(qsp.get("limit", "200")), 500))

        filters = []
        attr_values: dict = {}
        if cluster_filter:
            filters.append("cluster_id = :cid")
            attr_values[":cid"] = cluster_filter
        if actor_filter:
            # Match either requested_by or approved_by — DBA might be
            # asking "what did this person do?" not just "what did they
            # request?"
            filters.append("(requested_by = :a OR approved_by = :a)")
            attr_values[":a"] = actor_filter
        if action_filter:
            filters.append("(action_type = :at OR tool_name = :at)")
            attr_values[":at"] = action_filter

        scan_kwargs: dict = {}
        if filters:
            scan_kwargs["FilterExpression"] = " AND ".join(filters)
            scan_kwargs["ExpressionAttributeValues"] = attr_values

        response = table.scan(**scan_kwargs)
        items = sorted(
            response.get("Items", []),
            key=lambda x: x.get("created_at", ""),
            reverse=True,
        )[:limit]
        # Strip noisy fields so the activity feed stays scannable. The
        # action_details JSON can be huge for big DDL — keep a head
        # excerpt only.
        compact = []
        for it in items:
            details = it.get("action_details") or it.get("parameters") or {}
            if isinstance(details, str):
                details_str = details
            else:
                details_str = json.dumps(details, default=str)
            compact.append({
                "approval_id": it.get("approval_id"),
                "created_at": it.get("created_at"),
                "resolved_at": it.get("resolved_at"),
                "consumed_at": it.get("consumed_at"),
                "approval_status": it.get("approval_status"),
                "cluster_id": it.get("cluster_id"),
                "action_type": it.get("action_type") or it.get("tool_name"),
                "requested_by": it.get("requested_by"),
                "approved_by": it.get("approved_by"),
                "action_details_excerpt": details_str[:500],
            })
        return {
            "statusCode": 200,
            "headers": headers,
            "body": json.dumps({"items": compact, "count": len(compact)}, default=str),
        }

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
