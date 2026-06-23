import base64
import json
import os
import re
import traceback

import boto3


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _is_admin(event: dict) -> bool:
    """True if caller is admin (or has no group at all — default admin).
    False if explicitly in dbops-viewer or no token at all."""
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
    return True


def _forbid_viewer(event: dict):
    if _is_admin(event):
        return None
    return {
        "statusCode": 403,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({"error": "forbidden", "reason": "admin role required"}),
    }


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


_TZ_SUFFIX_RE = re.compile(r"(Z|[+-]\d{2}(:?\d{2})?)$")


def _norm_ts(s):
    """Normalize an RDS Data API timestamp string to unambiguous ISO 8601 UTC.

    The Data API returns TIMESTAMP / TIMESTAMPTZ as a space-separated, tz-less
    string in UTC (e.g. "2026-06-09 10:24:28.123"). The browser's `new Date()`
    parses that space form as LOCAL time, so every rendered timestamp came out
    shifted by the viewer's UTC offset (~9h in KST). Emit "...T...Z" so the
    client parses it as UTC and renders it in local time correctly. Strings
    that already carry a zone/offset are left untouched.
    """
    if not s or not isinstance(s, str):
        return s
    iso = s.replace(" ", "T", 1)
    if _TZ_SUFFIX_RE.search(iso):
        return iso
    return iso + "Z"


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
        meta = resp.get("columnMetadata", [])
        cols = [c["name"] for c in meta]
        # typeName per column, so we normalize ONLY timestamp columns (leaving
        # text that happens to look date-ish untouched).
        col_is_ts = ["timestamp" in (c.get("typeName") or "").lower() for c in meta]
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
                        val = f[typ]
                        if typ == "stringValue" and i < len(col_is_ts) and col_is_ts[i]:
                            val = _norm_ts(val)
                        row[col] = val
                        break
                else:
                    row[col] = None
            rows.append(row)
        return rows
    return query


def _list_rules(query, cluster_id):
    # LATERAL JOIN mirrors the evaluator window (10 min). data_status:
    #   fresh    — metric snapshot within 10 min, rule eligible to fire
    #   stale    — metric snapshot exists but older than 10 min, rule will skip
    #   no_data  — no snapshot at all, likely a misconfigured cluster_id/metric pair
    base = (
        "SELECT r.id, r.cluster_id, r.name, r.metric_type, r.comparison, r.threshold, r.enabled, "
        "  r.last_triggered_at, r.last_acked_at, r.last_acked_by, "
        "  r.created_at, r.conditions::text AS conditions_json, "
        "  m.latest_metric_ts, "
        "  CASE "
        "    WHEN m.latest_metric_ts IS NULL THEN 'no_data' "
        "    WHEN m.latest_metric_ts > NOW() - INTERVAL '10 minutes' THEN 'fresh' "
        "    ELSE 'stale' "
        "  END AS data_status "
        "FROM alert_rules r "
        "LEFT JOIN LATERAL ("
        "  SELECT MAX(ts) AS latest_metric_ts FROM metric_snapshots "
        "  WHERE cluster_id = r.cluster_id AND metric_type = r.metric_type"
        ") m ON true "
    )
    if cluster_id:
        rows = query(base + "WHERE r.cluster_id = :cid ORDER BY r.id DESC", {"cid": cluster_id})
    else:
        rows = query(base + "ORDER BY r.cluster_id, r.id DESC")
    return {"rules": rows}


def _validate_conditions(conditions: dict) -> str | None:
    """Return an error string if the compound DSL is malformed, else None."""
    if not isinstance(conditions, dict):
        return "conditions must be an object"
    logic = (conditions.get("logic") or "and").lower()
    if logic not in ("and", "or"):
        return "conditions.logic must be 'and' or 'or'"
    operands = conditions.get("operands")
    if not isinstance(operands, list) or len(operands) == 0:
        return "conditions.operands must be a non-empty array"
    if len(operands) > 8:
        return "conditions.operands capped at 8 entries for v1"
    for i, op in enumerate(operands):
        if not isinstance(op, dict):
            return f"operand[{i}] must be an object"
        if not METRIC_RE.match(str(op.get("metric_type") or "")):
            return f"operand[{i}].metric_type invalid"
        if op.get("comparison") not in COMP_OPS:
            return f"operand[{i}].comparison must be one of {sorted(COMP_OPS)}"
        try:
            float(op.get("threshold"))
        except (TypeError, ValueError):
            return f"operand[{i}].threshold must be numeric"
        win = op.get("window_minutes", 10)
        try:
            win_int = int(win)
            if win_int < 1 or win_int > 1440:
                return f"operand[{i}].window_minutes must be 1..1440"
        except (TypeError, ValueError):
            return f"operand[{i}].window_minutes must be an integer"
        if op.get("agg", "max") not in ("max", "min", "avg", "last"):
            return f"operand[{i}].agg must be max|min|avg|last"
    return None


def _create_rule(query, body):
    cluster_id = body.get("cluster_id", "")
    name = (body.get("name") or "alert rule").strip()[:255]
    enabled = bool(body.get("enabled", True))

    if not CLUSTER_ID_RE.match(cluster_id):
        return _response(400, {"error": "invalid cluster_id"})

    conditions = body.get("conditions")
    if conditions is not None:
        err = _validate_conditions(conditions)
        if err:
            return _response(400, {"error": err})
        # Persist a denormalised "first operand" copy into the legacy columns
        # so the data_status lookup join (which matches on metric_type) still
        # works and old read paths don't need conditional logic.
        first = conditions["operands"][0]
        metric_type = str(first["metric_type"])
        comparison = str(first["comparison"])
        threshold = float(first["threshold"])
        if not body.get("name"):
            logic_text = (conditions.get("logic") or "and").upper()
            name = (
                f"{first['metric_type']} {first['comparison']} {first['threshold']} "
                f"({logic_text}+{len(conditions['operands']) - 1})"
            )[:255]
        rows = query(
            "INSERT INTO alert_rules (cluster_id, name, metric_type, comparison, threshold, enabled, conditions) "
            "VALUES (:cid, :name, :metric, :comp, :threshold, :enabled, :conditions::jsonb) "
            "RETURNING id, cluster_id, name, metric_type, comparison, threshold, enabled, "
            "         conditions::text AS conditions_json, created_at",
            {
                "cid": cluster_id,
                "name": name,
                "metric": metric_type,
                "comp": comparison,
                "threshold": threshold,
                "enabled": enabled,
                "conditions": json.dumps(conditions),
            },
        )
        return _response(201, {"rule": rows[0] if rows else None})

    # Legacy single-threshold path — unchanged behaviour.
    metric_type = body.get("metric_type", "")
    comparison = body.get("comparison", "")
    threshold = body.get("threshold")
    if not body.get("name"):
        name = f"{metric_type} {comparison} {threshold}".strip()[:255]
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
        {
            "cid": cluster_id,
            "name": name,
            "metric": metric_type,
            "comp": comparison,
            "threshold": threshold,
            "enabled": enabled,
        },
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


_MANAGED_PROTOCOLS = {"slack-webhook", "pagerduty-events-v2", "teams-webhook"}


def _list_subscriptions(sns_client, topic_arn, query):
    """Combine SNS-native subscribers with DBOps-managed (Slack / PD) ones."""
    subs = []
    if topic_arn and sns_client:
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
                    "managed": False,
                })
            next_token = resp.get("NextToken")
            if not next_token:
                break

    # Managed (RDS-backed) subscribers — Slack / PagerDuty.
    try:
        rows = query(
            "SELECT id, protocol, endpoint, label, enabled FROM alert_subscribers_managed "
            "ORDER BY id ASC",
        )
        for r in rows:
            subs.append({
                # Prefix with 'mgmt:' so the delete path can distinguish.
                "subscription_arn": f"mgmt:{r['id']}",
                "protocol": r["protocol"],
                "endpoint": r["endpoint"],
                "label": r.get("label"),
                "enabled": r.get("enabled", True),
                "managed": True,
            })
    except Exception as e:
        # Table may not exist yet on first deploy — fall through.
        print(f"[alerts] list managed subscribers failed: {e}")

    return {"subscriptions": subs, "topic_arn": topic_arn or ""}


def _create_subscription(sns_client, topic_arn, body, query):
    protocol = (body.get("protocol") or "").strip().lower()
    endpoint = (body.get("endpoint") or "").strip()
    if not endpoint:
        return _response(400, {"error": "endpoint required"})

    # Managed protocols (Slack / PD) — stored in RDS, posted by evaluator.
    if protocol in _MANAGED_PROTOCOLS:
        if protocol == "slack-webhook" and not endpoint.startswith("https://hooks.slack.com/"):
            return _response(400, {"error": "slack-webhook endpoint must be https://hooks.slack.com/..."})
        if protocol == "teams-webhook" and not endpoint.startswith("https://"):
            return _response(400, {"error": "teams-webhook endpoint must be an https Teams Incoming Webhook URL"})
        if protocol == "pagerduty-events-v2" and len(endpoint) < 20:
            return _response(400, {"error": "pagerduty-events-v2 endpoint must be the integration key"})
        label = (body.get("label") or "").strip() or None
        rows = query(
            "INSERT INTO alert_subscribers_managed (protocol, endpoint, label) "
            "VALUES (:p, :e, :l) RETURNING id, protocol, endpoint, label, enabled",
            {"p": protocol, "e": endpoint, "l": label},
        )
        r = rows[0]
        return _response(201, {
            "subscription_arn": f"mgmt:{r['id']}",
            "protocol": r["protocol"],
            "endpoint": r["endpoint"],
            "label": r.get("label"),
            "managed": True,
        })

    # SNS-native protocols.
    if not topic_arn or not sns_client:
        return _response(503, {"error": "alert topic not configured"})
    if protocol not in {"email", "email-json", "sms", "https"}:
        return _response(400, {"error": "protocol must be one of: email, email-json, sms, https, slack-webhook, pagerduty-events-v2"})
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
        "managed": False,
        "note": "email/sms subscriptions need owner confirmation via the link AWS sends.",
    })


def _delete_subscription(sns_client, sub_arn, query):
    if not sub_arn:
        return _response(400, {"error": "sub_arn required"})
    # Managed subscriber path: "mgmt:<id>"
    if sub_arn.startswith("mgmt:"):
        try:
            row_id = int(sub_arn.split(":", 1)[1])
        except ValueError:
            return _response(400, {"error": "invalid managed sub_arn"})
        rows = query(
            "DELETE FROM alert_subscribers_managed WHERE id = :id RETURNING id",
            {"id": row_id},
        )
        if not rows:
            return _response(404, {"error": "not found"})
        return _response(200, {"unsubscribed": sub_arn})

    if not sns_client:
        return _response(503, {"error": "alert topic not configured"})
    if sub_arn == "PendingConfirmation":
        return _response(400, {"error": "subscription not yet confirmed; remove via AWS Console"})
    sns_client.unsubscribe(SubscriptionArn=sub_arn)
    return _response(200, {"unsubscribed": sub_arn})


def _rule_impact(query, rule_id):
    """Return the operational context around a rule's most-recent trigger.

    When an alert fires the DBA wants to know "what else was going on at
    that moment?" — top slow queries, other alerts that fired in the
    same window, recent ops events. This endpoint stitches those signals
    into a single response so the UI doesn't have to make N round-trips.

    Window: ±5 minutes around triggered_at (caller can widen via ?window=).
    """
    try:
        rule_id_int = int(rule_id)
    except (TypeError, ValueError):
        return _response(400, {"error": "rule_id must be integer"})

    rule_rows = query(
        "SELECT id, cluster_id, name, metric_type, comparison, threshold, "
        "       last_triggered_at FROM alert_rules WHERE id = :id",
        {"id": rule_id_int},
    )
    if not rule_rows:
        return _response(404, {"error": "rule not found"})
    rule = rule_rows[0]
    cluster_id = rule.get("cluster_id") or ""
    triggered_at = rule.get("last_triggered_at")
    if not triggered_at:
        return _response(
            200,
            {
                "rule": rule,
                "window": None,
                "info": "이 룰은 아직 발화 이력이 없습니다.",
                "top_slow_queries": [],
                "concurrent_events": [],
                "concurrent_alerts": [],
            },
        )

    # Top 5 slow queries that overlapped the trigger window. snapshot_time
    # is the pg_stat_statements snapshot point, so a query showing up here
    # was active in the same window.
    top_slow = query(
        "SELECT query_hash, LEFT(query_text, 200) AS query_excerpt, "
        "       MAX(calls) AS calls, MAX(total_time_ms) AS total_ms, "
        "       MAX(mean_time_ms) AS mean_ms "
        "FROM query_stats "
        "WHERE cluster_id = :cid "
        "  AND snapshot_time BETWEEN ((:tat)::timestamptz - INTERVAL '5 minutes') "
        "                        AND ((:tat)::timestamptz + INTERVAL '5 minutes') "
        "GROUP BY query_hash, query_text "
        "ORDER BY total_ms DESC NULLS LAST LIMIT 5",
        {"cid": cluster_id, "tat": triggered_at},
    )

    # Non-alert events in the same window (RDS events, scaling, vacuum,
    # backup completion etc.). Excludes 'alert' event_type to avoid
    # double-rendering — those go in concurrent_alerts below.
    concurrent_events = query(
        "SELECT event_time, event_type, severity, LEFT(message, 240) AS message "
        "FROM event_log "
        "WHERE cluster_id = :cid AND event_type <> 'alert' "
        "  AND event_time BETWEEN ((:tat)::timestamptz - INTERVAL '5 minutes') "
        "                     AND ((:tat)::timestamptz + INTERVAL '5 minutes') "
        "ORDER BY event_time DESC LIMIT 10",
        {"cid": cluster_id, "tat": triggered_at},
    )

    # Other rules that fired within the same window — useful for spotting
    # cascading failures (CPU spike + connection burst together).
    concurrent_alerts = query(
        "SELECT event_time, raw_event->>'rule_id' AS rule_id, "
        "       LEFT(message, 200) AS message "
        "FROM event_log "
        "WHERE cluster_id = :cid AND event_type = 'alert' "
        "  AND COALESCE(raw_event->>'rule_id', '') <> :self_id "
        "  AND event_time BETWEEN ((:tat)::timestamptz - INTERVAL '5 minutes') "
        "                     AND ((:tat)::timestamptz + INTERVAL '5 minutes') "
        "ORDER BY event_time DESC LIMIT 10",
        {
            "cid": cluster_id,
            "tat": triggered_at,
            "self_id": str(rule_id_int),
        },
    )

    return _response(
        200,
        {
            "rule": rule,
            "window": {
                "center": triggered_at,
                "minutes": 5,
            },
            "top_slow_queries": top_slow,
            "concurrent_events": concurrent_events,
            "concurrent_alerts": concurrent_alerts,
        },
    )


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
                return _response(200, _list_subscriptions(sns_client, topic_arn, query))
            if method == "POST":
                forbid = _forbid_viewer(event)
                if forbid:
                    return forbid
                return _create_subscription(sns_client, topic_arn, body, query)
            if method == "DELETE":
                forbid = _forbid_viewer(event)
                if forbid:
                    return forbid
                return _delete_subscription(sns_client, qs.get("sub_arn"), query)
            return _response(405, {"error": f"method {method} not allowed"})

        # /api/alerts/{id}/impact — operational context around the rule's
        # most recent firing. Read-only; no admin gate needed.
        if method == "GET" and raw_path.rstrip("/").endswith("/impact"):
            return _rule_impact(query, path_params.get("id"))

        if method == "GET":
            return _response(200, _list_rules(query, qs.get("cluster_id")))
        if method == "POST":
            forbid = _forbid_viewer(event)
            if forbid:
                return forbid
            return _create_rule(query, body)
        if method == "PATCH":
            forbid = _forbid_viewer(event)
            if forbid:
                return forbid
            return _update_rule(query, path_params.get("id"), body)
        if method == "DELETE":
            forbid = _forbid_viewer(event)
            if forbid:
                return forbid
            return _delete_rule(query, path_params.get("id"))
        return _response(405, {"error": f"method {method} not allowed"})
    except Exception:
        print(f"Alerts error on {method} {raw_path}: {traceback.format_exc()}")
        return _response(500, {"error": "Internal server error"})
