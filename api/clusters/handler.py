import json
import os
import boto3
from datetime import datetime


def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ["CLUSTERS_TABLE"])
    method = event.get("requestContext", {}).get("http", {}).get("method", event.get("httpMethod", "GET"))

    if method == "GET":
        response = table.scan()
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(response.get("Items", []), default=str),
        }

    if method == "POST":
        body = json.loads(event.get("body", "{}"))
        required = ["cluster_id", "account_id", "region"]
        for field in required:
            if field not in body:
                return {"statusCode": 400, "body": json.dumps({"error": f"{field} required"})}

        table.put_item(Item={
            "cluster_id": body["cluster_id"],
            "account_id": body["account_id"],
            "region": body["region"],
            "engine": body.get("engine", "aurora-postgresql"),
            "spoke_role_arn": body.get("spoke_role_arn", ""),
            "registered_at": datetime.utcnow().isoformat(),
        })
        return {
            "statusCode": 201,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"status": "registered", "cluster_id": body["cluster_id"]}),
        }

    return {"statusCode": 405, "body": json.dumps({"error": "Method not allowed"})}
