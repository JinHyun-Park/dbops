import json
import os
import re
import traceback
import boto3


COMP_OPS = {">", ">=", "<", "<=", "==", "!="}
CLUSTER_ID_RE = re.compile(r"^[a-zA-Z0-9-]{1,63}$")
METRIC_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,49}$")


_ALLOWED_ORIGINS = {
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if o.strip()
}

_CURRENT_ORIGIN = {"value": ""}


def _set_origin(event):
    headers = (event or {}).get("headers") or {}
    _CURRENT_ORIGIN["value"] = headers.get("origin") or headers.get("Origin") or ""


def _response(status, body):
    origin = _CURRENT_ORIGIN["value"]
    if _ALLOWED_ORIGINS:
        allow = origin if origin in _ALLOWED_ORIGINS else ""
    else:
        allow = origin or "*"
    cors = {}
    if allow:
        cors = {"Access-Control-Allow-Origin": allow}
        if allow != "*":
            cors["Vary"] = "Origin"
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            **cors,
        },
        "body": json.dumps(body, default=str),
    }


def _make_query(rds_data, cluster_arn, secret_arn, database):
    def query(sql, params=None):
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
            sql=f"/* source=dbops-alerts */ {sql}",
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
    return query


def _list_rules(query, cluster_id):
    if cluster_id:
        rows = query(
            "SELECT id, cluster_id, name, metric_type, comparison, threshold, enabled, "
            "  last_triggered_at, created_at "
            "FROM alert_rules WHERE cluster_id = :cid ORDER BY id DESC",
            {"cid": cluster_id},
        )
    else:
        rows = query(
            "SELECT id, cluster_id, name, metric_type, comparison, threshold, enabled, "
            "  last_triggered_at, created_at "
            "FROM alert_rules ORDER BY cluster_id, id DESC",
        )
    return {"rules": rows}


def _create_rule(query, body):
    cluster_id = body.get("cluster_id", "")
    metric_type = body.get("metric_type", "")
    comparison = body.get("comparison", "")
    threshold = body.get("threshold")
    name = (body.get("name") or f"{metric_type} {comparison} {threshold}").strip()[:255]
    enabled = bool(body.get("enabled", True))

    if not CLUSTER_ID_RE.match(cluster_id):
        return _response(400, {"error": "invalid cluster_id"})
    if not METRIC_RE.match(metric_type):
        return _response(400, {"error": "invalid metric_type"})
    if comparison not in COMP_OPS:
        return _response(400, {"error": f"comparison must be one of {sorted(COMP_OPS)}"})
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        return _response(400, {"error": "threshold must be numeric"})

    rows = query(
        "INSERT INTO alert_rules (cluster_id, name, metric_type, comparison, threshold, enabled) "
        "VALUES (:cid, :name, :metric, :comp, :threshold, :enabled) "
        "RETURNING id, cluster_id, name, metric_type, comparison, threshold, enabled, created_at",
        {"cid": cluster_id, "name": name, "metric": metric_type, "comp": comparison,
         "threshold": threshold, "enabled": enabled},
    )
    return _response(201, {"rule": rows[0] if rows else None})


def _update_rule(query, rule_id, body):
    try:
        rule_id_int = int(rule_id)
    except (TypeError, ValueError):
        return _response(400, {"error": "invalid id"})

    sets = []
    params = {"id": rule_id_int}
    if "enabled" in body:
        sets.append("enabled = :enabled")
        params["enabled"] = bool(body["enabled"])
    if "threshold" in body:
        try:
            params["threshold"] = float(body["threshold"])
            sets.append("threshold = :threshold")
        except (TypeError, ValueError):
            return _response(400, {"error": "threshold must be numeric"})
    if "name" in body:
        params["name"] = str(body["name"])[:255]
        sets.append("name = :name")
    if not sets:
        return _response(400, {"error": "no fields to update"})
    sets.append("updated_at = NOW()")

    rows = query(
        f"UPDATE alert_rules SET {', '.join(sets)} WHERE id = :id "
        "RETURNING id, cluster_id, name, metric_type, comparison, threshold, enabled",
        params,
    )
    if not rows:
        return _response(404, {"error": "not found"})
    return _response(200, {"rule": rows[0]})


def _list_subscriptions(sns_client, topic_arn):
    if not topic_arn or not sns_client:
        return {"subscriptions": [], "topic_arn": topic_arn or ""}
    subs = []
    next_token = None
    while True:
        kwargs = {"TopicArn": topic_arn}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = sns_client.list_subscriptions_by_topic(**kwargs)
        for s in resp.get("Subscriptions", []):
            subs.append({
                "subscription_arn": s.get("SubscriptionArn"),
                "protocol": s.get("Protocol"),
                "endpoint": s.get("Endpoint"),
            })
        next_token = resp.get("NextToken")
        if not next_token:
            break
    return {"subscriptions": subs, "topic_arn": topic_arn}


def _create_subscription(sns_client, topic_arn, body):
    if not topic_arn or not sns_client:
        return _response(503, {"error": "alert topic not configured"})
    protocol = (body.get("protocol") or "").strip().lower()
    endpoint = (body.get("endpoint") or "").strip()
    if protocol not in {"email", "email-json", "sms", "https"}:
        return _response(400, {"error": "protocol must be email, email-json, sms, or https"})
    if not endpoint:
        return _response(400, {"error": "endpoint required"})
    if protocol == "https" and not endpoint.startswith("https://"):
        return _response(400, {"error": "https endpoint must start with https://"})
    resp = sns_client.subscribe(
        TopicArn=topic_arn,
        Protocol=protocol,
        Endpoint=endpoint,
        ReturnSubscriptionArn=True,
    )
    return _response(201, {
        "subscription_arn": resp.get("SubscriptionArn"),
        "protocol": protocol,
        "endpoint": endpoint,
        "note": "email/sms subscriptions need owner confirmation via the link AWS sends.",
    })


def _delete_subscription(sns_client, sub_arn):
    if not sns_client:
        return _response(503, {"error": "alert topic not configured"})
    if not sub_arn or sub_arn == "PendingConfirmation":
        return _response(400, {"error": "subscription not yet confirmed; remove via AWS Console"})
    sns_client.unsubscribe(SubscriptionArn=sub_arn)
    return _response(200, {"unsubscribed": sub_arn})


def _delete_rule(query, rule_id):
    try:
        rule_id_int = int(rule_id)
    except (TypeError, ValueError):
        return _response(400, {"error": "invalid id"})
    rows = query(
        "DELETE FROM alert_rules WHERE id = :id RETURNING id",
        {"id": rule_id_int},
    )
    if not rows:
        return _response(404, {"error": "not found"})
    return _response(200, {"deleted": rule_id_int})


def lambda_handler(event, context):
    _set_origin(event)
    method = event.get("requestContext", {}).get("http", {}).get("method") \
        or event.get("httpMethod", "GET")
    raw_path = event.get("rawPath") or event.get("path") or ""
    qs = event.get("queryStringParameters") or {}
    path_params = event.get("pathParameters") or {}

    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")
    topic_arn = os.environ.get("ALERT_TOPIC_ARN", "")
    query = _make_query(boto3.client("rds-data"), cluster_arn, secret_arn, database)
    sns_client = boto3.client("sns") if topic_arn else None

    try:
        body = {}
        raw_body = event.get("body")
        if raw_body:
            body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body

        is_subscription_path = "alert-subscriptions" in raw_path

        if is_subscription_path:
            if method == "GET":
                return _response(200, _list_subscriptions(sns_client, topic_arn))
            if method == "POST":
                return _create_subscription(sns_client, topic_arn, body)
            if method == "DELETE":
                return _delete_subscription(sns_client, qs.get("sub_arn"))
            return _response(405, {"error": f"method {method} not allowed"})

        if method == "GET":
            return _response(200, _list_rules(query, qs.get("cluster_id")))
        if method == "POST":
            return _create_rule(query, body)
        if method == "PATCH":
            return _update_rule(query, path_params.get("id"), body)
        if method == "DELETE":
            return _delete_rule(query, path_params.get("id"))
        return _response(405, {"error": f"method {method} not allowed"})
    except Exception:
        print(f"Alerts error on {method} {raw_path}: {traceback.format_exc()}")
        return _response(500, {"error": "Internal server error"})
