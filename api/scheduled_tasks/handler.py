"""Scheduled Tasks REST API — list / create / delete recurring agent work.

CRUD over the `scheduled_tasks` cache table. The task_scheduler Lambda reads
these rows and enqueues agent-tasks when due. Read-only report schedules, so
any authenticated user may manage them (the route is behind the JWT authorizer).

See docs/superpowers/specs/2026-06-18-agent-tasks-design.md.
"""

import json
import os

import boto3

INTERVALS = {"hourly", "daily", "weekly"}
KINDS = {"scheduled_report"}


def _query(sql, params=None):
    rds_data = boto3.client("rds-data")
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")
    sql_params = []
    if params:
        for k, v in params.items():
            if isinstance(v, bool):
                sql_params.append({"name": k, "value": {"booleanValue": v}})
            elif isinstance(v, int):
                sql_params.append({"name": k, "value": {"longValue": v}})
            elif isinstance(v, float):
                sql_params.append({"name": k, "value": {"doubleValue": v}})
            else:
                sql_params.append({"name": k, "value": {"stringValue": str(v)}})
    resp = rds_data.execute_statement(
        resourceArn=cluster_arn,
        secretArn=secret_arn,
        database=database,
        sql=f"/* source=dbops-scheduled-tasks-api */ {sql}",
        parameters=sql_params,
        includeResultMetadata=True,
    )
    cols = [c["name"] for c in resp.get("columnMetadata", [])]
    rows = []
    for rec in resp.get("records", []):
        row = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) else f"col_{i}"
            if f.get("isNull"):
                row[col] = None
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in f:
                    row[col] = f[typ]
                    break
            else:
                row[col] = None
        rows.append(row)
    return rows


def _cluster_exists(cluster_id: str) -> bool:
    name = os.environ.get("CLUSTERS_TABLE", "")
    if not name:
        return True
    try:
        t = boto3.resource("dynamodb").Table(name)
        return "Item" in t.get_item(Key={"cluster_id": cluster_id})
    except Exception:
        return True  # fail-open on registry read error


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", event.get("httpMethod", "GET"))
    path_params = event.get("pathParameters") or {}
    qsp = event.get("queryStringParameters") or {}
    headers = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}
    sched_id = path_params.get("id")

    if method == "GET":
        cluster = (qsp.get("cluster") or "").strip()
        try:
            if cluster:
                rows = _query(
                    "SELECT id, cluster_id, kind, interval_kind, enabled, "
                    "last_run_at::text AS last_run_at, created_at::text AS created_at "
                    "FROM scheduled_tasks WHERE cluster_id = :cid ORDER BY created_at DESC",
                    {"cid": cluster},
                )
            else:
                rows = _query(
                    "SELECT id, cluster_id, kind, interval_kind, enabled, "
                    "last_run_at::text AS last_run_at, created_at::text AS created_at "
                    "FROM scheduled_tasks ORDER BY created_at DESC LIMIT 200"
                )
        except Exception as e:
            return {"statusCode": 500, "headers": headers, "body": json.dumps({"error": str(e)[:200]})}
        return {"statusCode": 200, "headers": headers, "body": json.dumps({"schedules": rows}, default=str)}

    if method == "POST":
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": "invalid JSON body"})}
        cluster_id = (body.get("cluster_id") or "").strip()
        interval_kind = (body.get("interval_kind") or "").strip()
        kind = (body.get("kind") or "scheduled_report").strip()
        if not cluster_id:
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": "cluster_id required"})}
        if interval_kind not in INTERVALS:
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": f"interval_kind must be one of {sorted(INTERVALS)}"})}
        if kind not in KINDS:
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": f"kind must be one of {sorted(KINDS)}"})}
        if not _cluster_exists(cluster_id):
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": f"unknown cluster {cluster_id}"})}
        try:
            rows = _query(
                "INSERT INTO scheduled_tasks (cluster_id, kind, interval_kind) "
                "VALUES (:cid, :kind, :ival) RETURNING id",
                {"cid": cluster_id, "kind": kind, "ival": interval_kind},
            )
        except Exception as e:
            return {"statusCode": 500, "headers": headers, "body": json.dumps({"error": str(e)[:200]})}
        new_id = rows[0].get("id") if rows else None
        return {
            "statusCode": 201,
            "headers": headers,
            "body": json.dumps(
                {"id": new_id, "cluster_id": cluster_id, "kind": kind, "interval_kind": interval_kind},
                default=str,
            ),
        }

    if method == "DELETE" and sched_id:
        try:
            _query("DELETE FROM scheduled_tasks WHERE id = :id", {"id": int(sched_id)})
        except (ValueError, TypeError):
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": "invalid id"})}
        except Exception as e:
            return {"statusCode": 500, "headers": headers, "body": json.dumps({"error": str(e)[:200]})}
        return {"statusCode": 200, "headers": headers, "body": json.dumps({"deleted": sched_id})}

    return {"statusCode": 405, "headers": headers, "body": json.dumps({"error": "Method not allowed"})}
