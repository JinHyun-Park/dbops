import json
import os
import boto3
from datetime import datetime


def _enrich_with_meta(clusters):
    if not clusters:
        return clusters
    rds_data = boto3.client("rds-data")
    cluster_arn = os.environ.get("CACHE_DB_CLUSTER_ARN", "")
    secret_arn = os.environ.get("CACHE_DB_SECRET_ARN", "")
    db = os.environ.get("CACHE_DB_NAME", "dbops")
    if not (cluster_arn and secret_arn):
        return clusters

    try:
        ids = [c["cluster_id"] for c in clusters]
        in_clause = ",".join([f":id{i}" for i in range(len(ids))])
        params = [{"name": f"id{i}", "value": {"stringValue": cid}} for i, cid in enumerate(ids)]
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=db,
            sql=f"SELECT cluster_id, status, engine_version, storage_size_gb FROM cluster_meta WHERE cluster_id IN ({in_clause})",
            parameters=params,
            includeResultMetadata=True,
        )
        cols = [c["name"] for c in resp.get("columnMetadata", [])]
        meta_by_id = {}
        for rec in resp.get("records", []):
            row = {}
            for i, f in enumerate(rec):
                col = cols[i]
                for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                    if typ in f:
                        row[col] = f[typ]
                        break
            if row.get("cluster_id"):
                meta_by_id[row["cluster_id"]] = row
        for c in clusters:
            m = meta_by_id.get(c["cluster_id"], {})
            if m.get("status"):
                c["status"] = m["status"]
            if m.get("engine_version"):
                c["engine_version"] = m["engine_version"]
            if m.get("storage_size_gb") is not None:
                c["storage_size_gb"] = m["storage_size_gb"]
    except Exception as e:
        print(f"enrich error: {e}")
    return clusters


def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ["CLUSTERS_TABLE"])
    method = event.get("requestContext", {}).get("http", {}).get("method", event.get("httpMethod", "GET"))

    if method == "GET":
        response = table.scan()
        items = response.get("Items", [])
        items = _enrich_with_meta(items)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(items, default=str),
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
