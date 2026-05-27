import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

COMP_FN = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}

_AGG_FN = {"max": "MAX", "min": "MIN", "avg": "AVG", "last": "MAX"}  # 'last' approximates via MAX(ts)+value lookup; legacy = MAX


def _evaluate_operand(query_fn, cluster_id: str, op: dict) -> tuple[bool, float | None, str]:
    """Resolve a single operand to (matched, observed_value, summary_text).

    Operand shape: {metric_type, comparison, threshold, window_minutes, agg}
    """
    metric = op.get("metric_type")
    comp = op.get("comparison")
    threshold = op.get("threshold")
    window_min = int(op.get("window_minutes") or 10)
    agg = (op.get("agg") or "max").lower()
    if not metric or comp not in COMP_FN or threshold is None:
        return False, None, f"invalid operand: {op}"
    sql_agg = _AGG_FN.get(agg, "MAX")
    rows = query_fn(
        f"SELECT {sql_agg}(value) AS v "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = :mt "
        "AND ts > NOW() - (:win || ' minutes')::interval",
        {"cid": cluster_id, "mt": metric, "win": str(window_min)},
    )
    if not rows or rows[0].get("v") is None:
        return False, None, f"{metric}: no data"
    obs = float(rows[0]["v"])
    matched = COMP_FN[comp](obs, float(threshold))
    summary = f"{metric}({agg},{window_min}m)={obs:.2f} {comp} {threshold}"
    return matched, obs, summary


def _evaluate_conditions(
    query_fn, cluster_id: str, conditions: dict
) -> tuple[bool, list[str]]:
    """Evaluate the compound conditions DSL. Returns (overall_match, summaries)."""
    logic = (conditions.get("logic") or "and").lower()
    operands = conditions.get("operands") or []
    if not operands:
        return False, ["no operands"]

    results: list[tuple[bool, str]] = []
    for op in operands:
        matched, _, summary = _evaluate_operand(query_fn, cluster_id, op)
        results.append((matched, summary))

    if logic == "or":
        overall = any(r[0] for r in results)
    else:
        # default = and
        overall = all(r[0] for r in results)
    summaries = [s for _, s in results]
    return overall, summaries


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


def _dashboard_url(rule: dict, path: str = "/dashboard") -> str:
    """Build a deep link into the DBOps console. Returns empty if FRONTEND_URL
    is not configured so callers can drop the link gracefully."""
    base = os.environ.get("FRONTEND_URL", "").rstrip("/")
    if not base:
        return ""
    qs = urllib.parse.urlencode({
        "cluster": rule["cluster_id"],
        "alert_id": rule["id"],
    })
    return f"{base}{path}?{qs}"


def _build_slack_payload(rule: dict, latest: float) -> dict:
    """Slack Block Kit — color-coded section + dashboard/alerts buttons."""
    dashboard = _dashboard_url(rule, "/dashboard")
    alerts = _dashboard_url(rule, "/alerts")
    blocks: list[dict] = [
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
    ]
    # Deep-link buttons — only when FRONTEND_URL is set so users without a
    # deployed CloudFront domain still get a usable message.
    if dashboard:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open dashboard"},
                    "url": dashboard,
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open alerts"},
                    "url": alerts,
                },
            ],
        })
    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"rule_id `{rule['id']}` · evaluated by dbops-alert-evaluator"},
        ],
    })
    return {
        "blocks": blocks,
        # Fallback for clients that don't render blocks (push notifications).
        "text": f"DBOps alert: {rule['name']} ({rule['metric_type']}={latest:.2f} {rule['comparison']} {rule['threshold']}) on {rule['cluster_id']}",
    }


def _dedup_window_seconds() -> int:
    """How many seconds make up one PagerDuty dedup bucket. Configurable via
    env var so a deploy can tune flapping behavior without a code change."""
    try:
        minutes = int(os.environ.get("ALERT_DEDUP_WINDOW_MINUTES", "30"))
    except ValueError:
        minutes = 30
    return max(60, minutes * 60)  # floor at 1 minute to avoid pathological values


def _build_pagerduty_payload(rule: dict, latest: float, integration_key: str) -> dict:
    """PagerDuty Events API v2 — dedup_key now uses a TTL bucket so a flapping
    rule re-opens an incident every ALERT_DEDUP_WINDOW_MINUTES (default 30m)
    instead of being silenced for the lifetime of a single incident."""
    window = _dedup_window_seconds()
    bucket = int(time.time()) // window
    dashboard = _dashboard_url(rule, "/dashboard")
    links = []
    if dashboard:
        links.append({"href": dashboard, "text": "Open dashboard"})
        links.append({"href": _dashboard_url(rule, "/alerts"), "text": "Open alerts"})
    return {
        "routing_key": integration_key,
        "event_action": "trigger",
        "dedup_key": f"dbops-rule-{rule['id']}-w{bucket}",
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
                "dedup_window_minutes": window // 60,
            },
        },
        "links": links,
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
        "SELECT id, cluster_id, name, metric_type, comparison, threshold, conditions::text AS conditions_json "
        "FROM alert_rules WHERE enabled = true"
    )

    sns_topic = os.environ.get("ALERT_SNS_TOPIC_ARN", "")
    sns_client = boto3.client("sns") if sns_topic else None

    triggered = 0
    skipped = 0

    for rule in rules:
        rule_id = int(rule["id"])
        conditions_json = rule.get("conditions_json")
        compound = None
        if conditions_json:
            try:
                compound = json.loads(conditions_json)
            except (TypeError, ValueError) as e:
                print(f"[alert-eval] rule {rule_id} has malformed conditions: {e}")
                compound = None

        if compound:
            # Compound DSL path. `latest` for downstream Slack/PD payloads is
            # the observed value of the first operand (most rules only have
            # one); legacy notifiers stay happy with a single scalar.
            matched, summaries = _evaluate_conditions(q, rule["cluster_id"], compound)
            if not matched:
                if all("no data" in s for s in summaries):
                    skipped += 1
                continue
            # Pull a representative observed value — re-resolve the first
            # operand so we have a number to embed in the notification text.
            first_op = (compound.get("operands") or [{}])[0]
            _, latest, _ = _evaluate_operand(q, rule["cluster_id"], first_op)
            latest = latest if latest is not None else 0.0
            joiner = (
                " AND " if (compound.get("logic") or "and").lower() == "and" else " OR "
            )
            message = f"{rule['name']}: {joiner.join(summaries)}"
        else:
            # Legacy single-threshold path — unchanged.
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
            message = (
                f"{rule['name']}: {rule['metric_type']} = {latest:.2f} "
                f"{rule['comparison']} {threshold}"
            )

        # Build the audit-log payload — compound rules carry their full DSL,
        # legacy rules just the single threshold. Either way every alert keeps
        # full reproducibility of what fired.
        raw_event = {
            "rule_id": rule_id,
            "cluster_id": rule["cluster_id"],
            "observed_value": latest,
        }
        if compound:
            raw_event["conditions"] = compound
            raw_event["operand_summaries"] = summaries
        else:
            raw_event["metric_type"] = rule["metric_type"]
            raw_event["threshold"] = threshold
            raw_event["comparison"] = rule["comparison"]

        q(
            "INSERT INTO event_log (cluster_id, event_time, event_type, source, severity, message, raw_event) "
            "VALUES (:cid, NOW(), 'alert', 'dbops-alert-evaluator', 'warning', :msg, :raw::jsonb)",
            {
                "cid": rule["cluster_id"],
                "msg": message,
                "raw": json.dumps(raw_event),
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
