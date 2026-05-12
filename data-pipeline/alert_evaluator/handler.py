import json
import os
import urllib.request
import urllib.error
import boto3


COMP_FN = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _post_json(url: str, payload: dict, timeout: int = 5) -> tuple[int, str]:
    """POST JSON to a webhook URL. Returns (status_code, body_excerpt)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "dbops-alert-evaluator/1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(512).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(512).decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
    except Exception as e:
        return 0, str(e)[:200]


def _build_slack_payload(rule: dict, latest: float) -> dict:
    """Slack Block Kit — color-coded section with cluster, metric, threshold."""
    return {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🚨 DBOps alert: {rule['cluster_id']}"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Rule:*\n{rule['name']}"},
                    {"type": "mrkdwn", "text": f"*Metric:*\n`{rule['metric_type']}`"},
                    {
                        "type": "mrkdwn",
                        "text": f"*Threshold:*\n`{rule['comparison']} {rule['threshold']}`",
                    },
                    {"type": "mrkdwn", "text": f"*Observed:*\n`{latest:.2f}`"},
                ],
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"rule_id `{rule['id']}` · evaluated by dbops-alert-evaluator"},
                ],
            },
        ],
        # Fallback for clients that don't render blocks (push notifications).
        "text": f"DBOps alert: {rule['name']} ({rule['metric_type']}={latest:.2f} {rule['comparison']} {rule['threshold']}) on {rule['cluster_id']}",
    }


def _build_pagerduty_payload(rule: dict, latest: float, integration_key: str) -> dict:
    """PagerDuty Events API v2 — dedup_key=rule_id so a flapping rule groups."""
    return {
        "routing_key": integration_key,
        "event_action": "trigger",
        "dedup_key": f"dbops-rule-{rule['id']}",
        "payload": {
            "summary": f"{rule['name']}: {rule['metric_type']}={latest:.2f} {rule['comparison']} {rule['threshold']}",
            "source": rule["cluster_id"],
            "severity": "warning",
            "component": rule["metric_type"],
            "class": "dbops-alert",
            "custom_details": {
                "rule_id": rule["id"],
                "cluster_id": rule["cluster_id"],
                "metric_type": rule["metric_type"],
                "comparison": rule["comparison"],
                "threshold": rule["threshold"],
                "observed_value": latest,
            },
        },
    }


def _fanout_managed(query, rule: dict, latest: float) -> None:
    """Read managed (Slack / PagerDuty) subscribers from RDS and POST to each.
    Failures are recorded back to the row but don't abort the loop."""
    try:
        subs = query(
            "SELECT id, protocol, endpoint FROM alert_subscribers_managed WHERE enabled = true",
        )
    except Exception as e:
        # Table may not exist on legacy deploys; skip silently.
        print(f"[alert-eval] managed-subs query failed: {e}")
        return
    if not subs:
        return
    for s in subs:
        sub_id = s["id"]
        protocol = s["protocol"]
        endpoint = s["endpoint"]
        if protocol == "slack-webhook":
            payload = _build_slack_payload(rule, latest)
            url = endpoint
        elif protocol == "pagerduty-events-v2":
            payload = _build_pagerduty_payload(rule, latest, endpoint)
            url = "https://events.pagerduty.com/v2/enqueue"
        else:
            continue
        status, body = _post_json(url, payload)
        ok = 200 <= status < 300
        try:
            if ok:
                query(
                    "UPDATE alert_subscribers_managed SET last_used_at = NOW(), last_error = NULL WHERE id = :id",
                    {"id": sub_id},
                )
            else:
                query(
                    "UPDATE alert_subscribers_managed SET last_used_at = NOW(), last_error = :err WHERE id = :id",
                    {"id": sub_id, "err": f"{protocol} HTTP {status}: {body[:200]}"},
                )
        except Exception as e:
            print(f"[alert-eval] status update for sub {sub_id} failed: {e}")


def _query(rds_data, cluster_arn, secret_arn, database, sql, params=None):
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
        sql=f"/* source=dbops-alert-eval */ {sql}",
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


def lambda_handler(event, context):
    rds_data = boto3.client("rds-data")
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")

    def q(sql, params=None):
        return _query(rds_data, cluster_arn, secret_arn, database, sql, params)

    rules = q(
        "SELECT id, cluster_id, name, metric_type, comparison, threshold "
        "FROM alert_rules WHERE enabled = true"
    )

    sns_topic = os.environ.get("ALERT_SNS_TOPIC_ARN", "")
    sns_client = boto3.client("sns") if sns_topic else None

    triggered = 0
    skipped = 0

    for rule in rules:
        metric_rows = q(
            "SELECT MAX(value) AS latest_value "
            "FROM metric_snapshots "
            "WHERE cluster_id = :cid "
            "AND metric_type = :mt "
            "AND ts > NOW() - INTERVAL '10 minutes'",
            {"cid": rule["cluster_id"], "mt": rule["metric_type"]},
        )
        if not metric_rows or metric_rows[0].get("latest_value") is None:
            skipped += 1
            continue

        latest = float(metric_rows[0]["latest_value"])
        threshold = float(rule["threshold"])
        comp_fn = COMP_FN.get(rule["comparison"])
        if not comp_fn or not comp_fn(latest, threshold):
            continue

        rule_id = int(rule["id"])
        message = (
            f"{rule['name']}: {rule['metric_type']} = {latest:.2f} "
            f"{rule['comparison']} {threshold}"
        )

        q(
            "INSERT INTO event_log (cluster_id, event_time, event_type, source, severity, message, raw_event) "
            "VALUES (:cid, NOW(), 'alert', 'dbops-alert-evaluator', 'warning', :msg, :raw::jsonb)",
            {
                "cid": rule["cluster_id"],
                "msg": message,
                "raw": json.dumps({
                    "rule_id": rule_id,
                    "metric_type": rule["metric_type"],
                    "value": latest,
                    "threshold": threshold,
                    "comparison": rule["comparison"],
                }),
            },
        )

        q(
            "UPDATE alert_rules SET last_triggered_at = NOW() WHERE id = :id",
            {"id": rule_id},
        )

        if sns_client:
            try:
                sns_client.publish(
                    TopicArn=sns_topic,
                    Subject=f"DBOps Alert: {rule['cluster_id']}",
                    Message=message,
                )
            except Exception as e:
                print(f"SNS publish failed for rule {rule_id}: {e}")

        # Slack / PagerDuty fan-out (RDS-backed subscribers).
        _fanout_managed(q, rule, latest)

        triggered += 1

    return {
        "statusCode": 200,
        "body": json.dumps({
            "rules_evaluated": len(rules),
            "triggered": triggered,
            "skipped": skipped,
        }),
    }
