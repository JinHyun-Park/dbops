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
    item = _get_target(target_id)
    if not item:
        return _resp(404, {"error": "not found"})
    if not _target_visible(event, item):
        return _resp(403, {"error": "forbidden"})
    rows = _execute(
        "SELECT DISTINCT ON (metric_type) metric_type, value "
        "FROM apm_metric_snapshots WHERE target_id = :tid "
        "ORDER BY metric_type, ts DESC",
        {"tid": target_id})
    metrics = {r["metric_type"]: r["value"] for r in rows}
    log_rows = _execute(
        "SELECT level, COALESCE(SUM(count),0) AS total FROM apm_log_level_counts "
        "WHERE target_id = :tid AND ts > NOW() - INTERVAL '1 hour' GROUP BY level",
        {"tid": target_id})
    log_counts = {r["level"]: int(r["total"]) for r in log_rows}
    return _resp(200, {"target_id": target_id, "metrics": metrics, "log_counts": log_counts})


def _metrics(event, target_id):
    item = _get_target(target_id)
    if not item:
        return _resp(404, {"error": "not found"})
    if not _target_visible(event, item):
        return _resp(403, {"error": "forbidden"})
    qs = event.get("queryStringParameters") or {}
    metric_type = qs.get("metric_type", "cpu")
    try:
        hours = max(1, min(168, int(qs.get("hours", "6"))))
    except ValueError:
        hours = 6
    rows = _execute(
        f"SELECT ts, value FROM apm_metric_snapshots "
        f"WHERE target_id = :tid AND metric_type = :mt "
        f"AND ts > NOW() - INTERVAL '{hours} hours' ORDER BY ts",
        {"tid": target_id, "mt": metric_type})
    return _resp(200, {"target_id": target_id, "metric_type": metric_type, "series": rows})

_DEFAULT_LEVELS = ["ERROR", "WARN"]


def _levels_filter(levels):
    """Server-side level gate. Default ERROR+WARN to avoid unbounded scans."""
    import re
    lv = [re.sub(r"[^A-Z]", "", (x or "").upper()) for x in (levels or _DEFAULT_LEVELS)]
    lv = [x for x in lv if x] or _DEFAULT_LEVELS
    ors = " or ".join(f"@message like /{x}/" for x in lv)
    return f"filter ({ors})"


def _session_for(region="", role_arn=""):
    region = region or os.environ.get("AWS_REGION", "")
    if not role_arn:
        return boto3.session.Session(region_name=region or None)
    creds = boto3.client("sts").assume_role(
        RoleArn=role_arn, RoleSessionName="dbops-apm", DurationSeconds=900,
    )["Credentials"]
    return boto3.session.Session(
        region_name=region or None,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"])


def _logs_client_for(item):
    return _session_for(item.get("region", ""), item.get("spoke_role_arn", "")).client("logs")


def _logs_search(event, target_id):
    import re
    item = _get_target(target_id)
    if not item:
        return _resp(404, {"error": "not found"})
    if not _target_visible(event, item):
        return _resp(403, {"error": "forbidden"})
    body = json.loads(event.get("body") or "{}")
    log_group = body.get("log_group") or (item.get("log_groups") or [""])[0]
    if not log_group:
        return _resp(400, {"error": "no log_group for target"})
    try:
        hours = max(1, min(48, int(body.get("hours", 1))))
    except (ValueError, TypeError):
        hours = 1
    limit = min(int(body.get("limit", 100) or 100), 500)

    parts = [_levels_filter(body.get("levels"))]
    for raw in (body.get("query") or "").split():
        cleaned = re.sub(r"[^A-Za-z0-9_./:\-]", "", raw)
        if cleaned:
            parts.append(f"filter @message like /{cleaned}/")
    query_string = ("fields @timestamp, @message | " + " | ".join(parts)
                    + f" | sort @timestamp desc | limit {limit}")

    client = _logs_client_for(item)
    base = {"target_id": target_id, "log_group": log_group,
            "compiled_query": query_string, "entries": [], "count": 0}
    try:
        qid = client.start_query(
            logGroupName=log_group,
            startTime=int(time.time() - hours * 3600),
            endTime=int(time.time()),
            queryString=query_string)["queryId"]
    except Exception as e:
        return _resp(200, {**base, "error": f"start_query failed: {e}"})
    for _ in range(25):
        r = client.get_query_results(queryId=qid)
        status = r.get("status")
        if status == "Complete":
            entries = []
            for row in r.get("results", []) or []:
                fields = {f["field"]: f["value"] for f in row}
                entries.append({"ts": fields.get("@timestamp"),
                                "message": fields.get("@message", "")})
            return _resp(200, {**base, "entries": entries, "count": len(entries)})
        if status in ("Failed", "Cancelled"):
            return _resp(200, {**base, "error": f"query {status.lower()}"})
        time.sleep(1)
    return _resp(200, {**base, "error": "query timed out"})
