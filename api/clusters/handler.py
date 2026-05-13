"""Clusters API.

Routes:
  GET  /api/clusters              — list registered clusters (existing)
  POST /api/clusters              — register one cluster (existing)
  POST /api/clusters/discover     — list candidate clusters in an account+region
  POST /api/clusters/bulk-register — register multiple discovered clusters
"""

import base64
import json
import os
import boto3
from botocore.exceptions import ClientError
from datetime import datetime

import seeder


def _decode_jwt_payload(token: str) -> dict:
    """Decode a JWT payload (base64) — no signature verification.
    Cognito-issued tokens originate from a trusted client we control, and a
    follow-up task wires API Gateway JWT authorizer for proper verification.
    Here we just want the `cognito:groups` claim for RBAC."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _is_admin(event: dict) -> bool:
    """Return True if the caller's token does not place them in dbops-viewer.
    Tokens without any group claim default to admin (one-admin deploys), matching
    the frontend isAdmin() semantics. Anonymous (no token) requests are NOT
    considered admin — they fall through to 403 in callers that gate writes."""
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    claims = _decode_jwt_payload(auth.split(" ", 1)[1])
    groups = claims.get("cognito:groups") or []
    if not isinstance(groups, list):
        return False
    if "dbops-viewer" in groups and "dbops-admin" not in groups:
        return False
    # Token present + not explicitly viewer → admin.
    return True


def _forbid_viewer(event: dict):
    """Return a 403 response if the caller is a viewer, else None."""
    if _is_admin(event):
        return None
    return {
        "statusCode": 403,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({"error": "forbidden", "reason": "admin role required"}),
    }


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


def _cors():
    return {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}


def _resp(status, body):
    return {"statusCode": status, "headers": _cors(), "body": json.dumps(body, default=str)}


def _rds_client_for(region: str, role_arn: str = ""):
    """Return an RDS client. If role_arn is given, assume it cross-account."""
    if not role_arn:
        return boto3.client("rds", region_name=region)
    sts = boto3.client("sts")
    creds = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"dbops-discover-{datetime.utcnow().strftime('%H%M%S')}",
        DurationSeconds=900,
    )["Credentials"]
    return boto3.client(
        "rds",
        region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _list_clusters_in_region(region: str, role_arn: str = "") -> list[dict]:
    """Enumerate Aurora clusters in a region. Returns one row per cluster."""
    rds = _rds_client_for(region, role_arn)
    out = []
    paginator = rds.get_paginator("describe_db_clusters")
    for page in paginator.paginate():
        for c in page.get("DBClusters", []):
            engine = c.get("Engine", "")
            if not engine.startswith("aurora"):
                # Skip non-Aurora RDS (we only support Aurora MySQL/PG today).
                continue
            master_secret = (c.get("MasterUserSecret") or {}).get("SecretArn", "")
            out.append({
                "cluster_id": c.get("DBClusterIdentifier", ""),
                "cluster_arn": c.get("DBClusterArn", ""),
                "engine": engine,
                "engine_version": c.get("EngineVersion", ""),
                "endpoint": c.get("Endpoint", ""),
                "status": c.get("Status", ""),
                "db_name": c.get("DatabaseName", "") or "postgres",
                "secret_arn": master_secret,
                "region": region,
            })
    return out


def _handle_list(table):
    response = table.scan()
    items = response.get("Items", [])
    items = _enrich_with_meta(items)
    return _resp(200, items)


def _handle_register(table, body: dict):
    required = ["cluster_id", "account_id", "region"]
    for field in required:
        if field not in body:
            return _resp(400, {"error": f"{field} required"})

    cluster_id = body["cluster_id"]
    account_id = body["account_id"]
    region = body["region"]
    spoke_role_arn = body.get("spoke_role_arn", "")
    connection_status = "untested"
    connection_error = ""

    # Validate access by calling DescribeDBClusters via local or assumed role.
    try:
        rds = _rds_client_for(region, spoke_role_arn)
        rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        connection_status = "ok"
    except Exception as e:
        connection_status = "failed"
        connection_error = str(e)[:300]

    item = {
        "cluster_id": cluster_id,
        "account_id": account_id,
        "region": region,
        "engine": body.get("engine", "aurora-postgresql"),
        "spoke_role_arn": spoke_role_arn,
        "registered_at": datetime.utcnow().isoformat(),
        "connection_status": connection_status,
        "connection_error": connection_error,
        "connection_validated_at": datetime.utcnow().isoformat() if connection_status != "untested" else "",
    }
    # Carry over arn/secret/db_name when the caller already resolved them
    # (bulk-register path provides these; single-cluster manual entry may not).
    for k in ("cluster_arn", "secret_arn", "db_name"):
        if body.get(k):
            item[k] = body[k]

    table.put_item(Item=item)

    status_code = 201 if connection_status != "failed" else 207
    return _resp(status_code, {
        "status": "registered" if connection_status != "failed" else "registered_with_warning",
        "cluster_id": cluster_id,
        "connection_status": connection_status,
        "connection_error": connection_error,
    })


def _handle_discover(table, body: dict):
    region = body.get("region")
    regions = body.get("regions") or ([region] if region else [])
    if not regions:
        return _resp(400, {"error": "region or regions required"})
    role_arn = body.get("role_arn", "")
    account_id = body.get("account_id", "")  # informational; only used in output

    # Build a set of already-registered cluster_ids to flag duplicates.
    existing_ids = set()
    try:
        scan = table.scan(ProjectionExpression="cluster_id")
        existing_ids = {row["cluster_id"] for row in scan.get("Items", []) if row.get("cluster_id")}
    except Exception as e:
        print(f"[discover] dedupe scan failed: {e}")

    all_clusters = []
    errors_by_region = {}
    for r in regions:
        try:
            rows = _list_clusters_in_region(r, role_arn)
            for row in rows:
                row["already_registered"] = row["cluster_id"] in existing_ids
                row["account_id"] = account_id
            all_clusters.extend(rows)
        except ClientError as e:
            errors_by_region[r] = e.response.get("Error", {}).get("Code", str(e))
        except Exception as e:
            errors_by_region[r] = str(e)[:200]

    return _resp(200, {
        "clusters": all_clusters,
        "errors": errors_by_region,
        "scanned_regions": regions,
    })


def _handle_bulk_register(table, body: dict):
    clusters = body.get("clusters") or []
    if not isinstance(clusters, list) or not clusters:
        return _resp(400, {"error": "clusters[] required"})

    registered, skipped, failed = [], [], []
    for c in clusters:
        try:
            cluster_id = c.get("cluster_id")
            if not cluster_id:
                failed.append({"cluster_id": "(missing)", "error": "cluster_id missing"})
                continue
            # Skip already-registered unless caller passes force=true.
            existing = table.get_item(Key={"cluster_id": cluster_id}).get("Item")
            if existing and not c.get("force"):
                skipped.append({"cluster_id": cluster_id, "reason": "already_registered"})
                continue
            sub_body = {
                "cluster_id": cluster_id,
                "account_id": c.get("account_id", ""),
                "region": c.get("region"),
                "engine": c.get("engine", "aurora-postgresql"),
                "spoke_role_arn": c.get("spoke_role_arn", ""),
                "cluster_arn": c.get("cluster_arn", ""),
                "secret_arn": c.get("secret_arn", ""),
                "db_name": c.get("db_name", ""),
            }
            resp = _handle_register(table, sub_body)
            payload = json.loads(resp["body"])
            registered.append({
                "cluster_id": cluster_id,
                "connection_status": payload.get("connection_status"),
            })
        except Exception as e:
            failed.append({"cluster_id": c.get("cluster_id", "?"), "error": str(e)[:200]})

    return _resp(200, {
        "registered": registered,
        "skipped": skipped,
        "failed": failed,
        "counts": {
            "registered": len(registered),
            "skipped": len(skipped),
            "failed": len(failed),
        },
    })


def _cache_db_env():
    return (
        os.environ.get("CACHE_DB_CLUSTER_ARN", ""),
        os.environ.get("CACHE_DB_SECRET_ARN", ""),
        os.environ.get("CACHE_DB_NAME", "dbops"),
    )


def _handle_seed_sample(table):
    """P1.4 Sample data / demo mode. Idempotent — re-running upserts the demo cluster."""
    cluster_arn, secret_arn, db_name = _cache_db_env()
    if not (cluster_arn and secret_arn):
        return _resp(500, {"error": "cache DB not configured"})

    cluster_id = seeder.SAMPLE_CLUSTER_ID
    rds_data = boto3.client("rds-data")
    try:
        counts = seeder.seed_demo_data(rds_data, cluster_arn, secret_arn, db_name, cluster_id)
    except Exception as e:
        return _resp(500, {"error": "seed_failed", "detail": str(e)[:300]})

    item = {
        "cluster_id": cluster_id,
        "account_id": "000000000000",
        "region": "ap-northeast-2",
        "engine": seeder.SAMPLE_ENGINE,
        "spoke_role_arn": "",
        "registered_at": datetime.utcnow().isoformat(),
        "connection_status": "ok",
        "connection_error": "",
        "connection_validated_at": datetime.utcnow().isoformat(),
        "is_demo": True,
    }
    table.put_item(Item=item)
    return _resp(201, {
        "status": "seeded",
        "cluster_id": cluster_id,
        "is_demo": True,
        "rows": counts,
    })


def _handle_delete(table, cluster_id: str):
    """DELETE /api/clusters/{cluster_id}. For demo clusters, also wipes cache rows."""
    existing = table.get_item(Key={"cluster_id": cluster_id}).get("Item")
    if not existing:
        return _resp(404, {"error": "not_found", "cluster_id": cluster_id})
    if existing.get("is_demo"):
        cluster_arn, secret_arn, db_name = _cache_db_env()
        if cluster_arn and secret_arn:
            try:
                seeder.cleanup_demo_data(boto3.client("rds-data"), cluster_arn, secret_arn, db_name, cluster_id)
            except Exception as e:
                print(f"[delete] cleanup_demo_data failed: {e}")
    table.delete_item(Key={"cluster_id": cluster_id})
    return _resp(200, {"status": "deleted", "cluster_id": cluster_id, "was_demo": bool(existing.get("is_demo"))})


def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ["CLUSTERS_TABLE"])
    method = event.get("requestContext", {}).get("http", {}).get("method", event.get("httpMethod", "GET"))
    path = event.get("rawPath") or event.get("requestContext", {}).get("http", {}).get("path", "")

    # Sub-route: POST /api/clusters/discover (read-only enumeration, allow viewer)
    if method == "POST" and path.endswith("/discover"):
        try:
            body = json.loads(event.get("body") or "{}")
        except Exception:
            return _resp(400, {"error": "invalid JSON"})
        return _handle_discover(table, body)

    # Sub-route: POST /api/clusters/sample — write, admin only
    if method == "POST" and path.endswith("/sample"):
        forbid = _forbid_viewer(event)
        if forbid:
            return forbid
        return _handle_seed_sample(table)

    # Sub-route: DELETE /api/clusters/{cluster_id} — write, admin only
    if method == "DELETE":
        forbid = _forbid_viewer(event)
        if forbid:
            return forbid
        # cluster_id from path parameters (API Gateway v2 {id} variable) or query string fallback.
        params = event.get("pathParameters") or {}
        cluster_id = params.get("id") or (event.get("queryStringParameters") or {}).get("cluster_id")
        if not cluster_id:
            return _resp(400, {"error": "cluster_id required"})
        return _handle_delete(table, cluster_id)

    # Sub-route: POST /api/clusters/bulk-register — write, admin only
    if method == "POST" and path.endswith("/bulk-register"):
        forbid = _forbid_viewer(event)
        if forbid:
            return forbid
        try:
            body = json.loads(event.get("body") or "{}")
        except Exception:
            return _resp(400, {"error": "invalid JSON"})
        return _handle_bulk_register(table, body)

    if method == "GET":
        return _handle_list(table)

    if method == "POST":
        # Single-cluster registration — write, admin only.
        forbid = _forbid_viewer(event)
        if forbid:
            return forbid
        try:
            body = json.loads(event.get("body") or "{}")
        except Exception:
            return _resp(400, {"error": "invalid JSON"})
        return _handle_register(table, body)

    return _resp(405, {"error": "method not allowed"})
