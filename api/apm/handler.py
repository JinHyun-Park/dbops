"""APM REST API — targets registry CRUD, cache-first reads, on-demand log search.

Metrics/summaries are read from the Aurora PG cache. Log SEARCH is the one
on-demand path: it assumes the target's spoke role and queries CloudWatch Logs
at request time (mirrors api/dashboard _log_insights). Read-only against AWS.
"""
import json
import os
import time

import boto3

import tenancy

_TABLE = os.environ.get("APM_TARGETS_TABLE", "")


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _table():
    return boto3.resource("dynamodb").Table(_TABLE)


def _scan_targets():
    resp = _table().scan()
    items = resp.get("Items", [])
    while resp.get("LastEvaluatedKey"):
        resp = _table().scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return items


def _get_target(target_id):
    return _table().get_item(Key={"target_id": target_id}).get("Item")


def _target_visible(event, item):
    """Reuse generic tenancy primitives; APM targets carry an optional `team`."""
    if tenancy.is_admin(event):
        return True
    team = (item or {}).get("team")
    if not team:
        return True
    return team in tenancy.my_team_ids(tenancy.caller_username(event))


def _execute(sql, params=None):
    rds = boto3.client("rds-data")
    sql_params = []
    for k, v in (params or {}).items():
        if v is None:
            sql_params.append({"name": k, "value": {"isNull": True}})
        elif isinstance(v, bool):
            sql_params.append({"name": k, "value": {"booleanValue": v}})
        elif isinstance(v, int):
            sql_params.append({"name": k, "value": {"longValue": v}})
        elif isinstance(v, float):
            sql_params.append({"name": k, "value": {"doubleValue": v}})
        else:
            sql_params.append({"name": k, "value": {"stringValue": str(v)}})
    resp = rds.execute_statement(
        resourceArn=os.environ["CACHE_DB_CLUSTER_ARN"],
        secretArn=os.environ["CACHE_DB_SECRET_ARN"],
        database=os.environ.get("CACHE_DB_NAME", "dbops"),
        sql=f"/* source=dbops-apm */ {sql}",
        parameters=sql_params,
        includeResultMetadata=True,
    )
    meta = resp.get("columnMetadata", [])
    cols = [c.get("name") or c.get("label") or "" for c in meta]
    out = []
    for rec in resp.get("records", []):
        row = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) and cols[i] else f"col_{i}"
            if f.get("isNull"):
                row[col] = None
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in f:
                    row[col] = f[typ]
                    break
        out.append(row)
    return out


def _list(event):
    items = [t for t in _scan_targets() if _target_visible(event, t)]
    return _resp(200, {"targets": items})


def _create(event):
    if not tenancy.is_admin(event):
        return _resp(403, {"error": "admin only"})
    body = json.loads(event.get("body") or "{}")
    tid = body.get("target_id")
    if not tid:
        return _resp(400, {"error": "target_id required"})
    item = {
        "target_id": tid,
        "instance_id": body.get("instance_id", ""),
        "region": body.get("region", ""),
        "account_id": body.get("account_id", ""),
        "spoke_role_arn": body.get("spoke_role_arn", ""),
        "log_groups": body.get("log_groups") or [],
        "service_name": body.get("service_name", ""),
        "team": body.get("team", ""),
    }
    _table().put_item(Item=item)
    return _resp(201, item)


def _get_one(event, target_id):
    item = _get_target(target_id)
    if not item:
        return _resp(404, {"error": "not found"})
    if not _target_visible(event, item):
        return _resp(403, {"error": "forbidden"})
    return _resp(200, item)


def _update(event, target_id):
    if not tenancy.is_admin(event):
        return _resp(403, {"error": "admin only"})
    existing = _get_target(target_id)
    if not existing:
        return _resp(404, {"error": "not found"})
    body = json.loads(event.get("body") or "{}")
    existing.update({k: v for k, v in body.items() if k != "target_id"})
    _table().put_item(Item=existing)
    return _resp(200, existing)


def _delete(event, target_id):
    if not tenancy.is_admin(event):
        return _resp(403, {"error": "admin only"})
    _table().delete_item(Key={"target_id": target_id})
    return _resp(200, {"deleted": target_id})


def lambda_handler(event, context):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod") or "GET"
    )
    pp = event.get("pathParameters") or {}
    raw_path = event.get("rawPath") or (event.get("requestContext", {}).get("http", {}).get("path") or "")
    target_id = pp.get("id")

    # /api/apm/{id}/overview | /metrics | /logs/search  → Tasks 6-7
    if target_id and raw_path.endswith("/overview") and method == "GET":
        return _overview(event, target_id)
    if target_id and raw_path.endswith("/metrics") and method == "GET":
        return _metrics(event, target_id)
    if target_id and raw_path.endswith("/logs/search") and method == "POST":
        return _logs_search(event, target_id)

    # /api/apm/targets  and  /api/apm/targets/{id}
    if method == "GET" and not target_id:
        return _list(event)
    if method == "POST" and not target_id:
        return _create(event)
    if method == "GET" and target_id:
        return _get_one(event, target_id)
    if method == "PUT" and target_id:
        return _update(event, target_id)
    if method == "DELETE" and target_id:
        return _delete(event, target_id)
    return _resp(405, {"error": f"method {method} not allowed"})


def _overview(event, target_id):
    return _resp(501, {"error": "not implemented"})

def _metrics(event, target_id):
    return _resp(501, {"error": "not implemented"})

def _logs_search(event, target_id):
    return _resp(501, {"error": "not implemented"})
