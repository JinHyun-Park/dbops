"""Slack interactive endpoint — handles button clicks from outbound DBOps
alert messages.

The outbound side (data-pipeline/alert_evaluator) adds an "Ack" Block Kit
button to every Slack message it sends; clicking it makes Slack POST the
interaction back to /api/slack/interactive. We verify the request with
the Slack signing secret, then mark the alert as acknowledged on the row
and respond with an updated message so the button visibly flips to
"✓ Acked by @user at TIME" without anyone having to refresh the UI.

Signing model (Slack v0):
  X-Slack-Request-Timestamp + raw body → HMAC-SHA256 with
  SLACK_SIGNING_SECRET → "v0=<hex>" compared in constant time against
  X-Slack-Signature. Anything older than 5 minutes is rejected to
  defeat replay.

Failure modes return harmless 200/400 responses with a user-visible
message inside the Slack channel rather than a blank error, so the DBA
who clicked the button gets feedback.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import traceback
import urllib.parse

import boto3

_SIGNING_SECRET_ENV = "SLACK_SIGNING_SECRET"
_MAX_REQUEST_AGE_S = 60 * 5


def _resp(status: int, body: dict | str) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": body if isinstance(body, str) else json.dumps(body, default=str),
    }


def _verify_slack_signature(headers: dict, raw_body: str) -> tuple[bool, str]:
    secret = os.environ.get(_SIGNING_SECRET_ENV)
    if not secret:
        return False, "SLACK_SIGNING_SECRET not configured on this deployment"
    # Header lookup is case-insensitive in HTTP but Lambda gives them as-is.
    norm = {k.lower(): v for k, v in (headers or {}).items()}
    ts = norm.get("x-slack-request-timestamp", "")
    sig = norm.get("x-slack-signature", "")
    if not ts or not sig:
        return False, "missing X-Slack-Signature / X-Slack-Request-Timestamp"
    try:
        ts_int = int(ts)
    except ValueError:
        return False, "invalid timestamp"
    if abs(time.time() - ts_int) > _MAX_REQUEST_AGE_S:
        return False, "stale request — replay window exceeded"
    basestring = f"v0:{ts}:{raw_body}"
    expected = (
        "v0="
        + hmac.new(secret.encode(), basestring.encode(), hashlib.sha256).hexdigest()
    )
    if not hmac.compare_digest(expected, sig):
        return False, "signature mismatch"
    return True, ""


def _get_raw_body(event: dict) -> str:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except Exception:
            return ""
    return body


def _rds_data():
    return boto3.client("rds-data")


def _execute(sql: str, params: dict) -> list[dict]:
    sql_params = []
    for k, v in (params or {}).items():
        if v is None:
            sql_params.append({"name": k, "value": {"isNull": True}})
        elif isinstance(v, bool):
            sql_params.append({"name": k, "value": {"booleanValue": v}})
        elif isinstance(v, int):
            sql_params.append({"name": k, "value": {"longValue": v}})
        else:
            sql_params.append({"name": k, "value": {"stringValue": str(v)}})
    resp = _rds_data().execute_statement(
        resourceArn=os.environ["CACHE_DB_CLUSTER_ARN"],
        secretArn=os.environ["CACHE_DB_SECRET_ARN"],
        database=os.environ.get("CACHE_DB_NAME", "dbops"),
        sql=f"/* source=dbops-slack-interactive */ {sql}",
        parameters=sql_params,
        includeResultMetadata=True,
    )
    cols = [c.get("name") or c.get("label") or "" for c in resp.get("columnMetadata", [])]
    rows = []
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
        rows.append(row)
    return rows


def _acked_blocks(
    rule_name: str,
    cluster_id: str,
    rule_id: int,
    user_display: str,
    when_iso: str,
) -> dict:
    """Return the Slack response body that REPLACES the original message —
    same header, but the "Ack" button disappears and an acknowledgement
    section is appended so subsequent viewers see who took the page."""
    return {
        "replace_original": True,
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"DBOps alert ack: {cluster_id}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Rule:* {rule_name}\n"
                        f"*Acked by:* <@{user_display}>\n"
                        f"*At:* {when_iso}"
                    ),
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"rule_id `{rule_id}` · further triggers will "
                            "reset the ack state."
                        ),
                    }
                ],
            },
        ],
        "text": f"DBOps alert acked by {user_display}",
    }


def lambda_handler(event, context):
    raw_body = _get_raw_body(event)
    headers = event.get("headers") or {}

    ok, why = _verify_slack_signature(headers, raw_body)
    if not ok:
        # Return 200 with an ephemeral error message so the DBA sees why
        # the click was rejected (vs Slack showing the generic
        # "We had trouble connecting").
        return _resp(
            200,
            {
                "response_type": "ephemeral",
                "text": f":warning: DBOps could not verify this request — {why}",
            },
        )

    # Slack POSTs a urlencoded body with a single field `payload` whose
    # value is JSON. Anything else is a misconfigured app → ephemeral note.
    try:
        form = urllib.parse.parse_qs(raw_body)
        payload_raw = (form.get("payload") or [""])[0]
        payload = json.loads(payload_raw) if payload_raw else {}
    except (ValueError, TypeError):
        return _resp(
            200,
            {"response_type": "ephemeral", "text": ":warning: malformed payload"},
        )

    actions = payload.get("actions") or []
    if not actions:
        return _resp(
            200,
            {"response_type": "ephemeral", "text": ":warning: no action in payload"},
        )
    action = actions[0]
    action_id = action.get("action_id") or ""
    value = action.get("value") or ""

    if action_id != "ack_alert":
        return _resp(
            200,
            {
                "response_type": "ephemeral",
                "text": f":warning: unknown action `{action_id}`",
            },
        )

    # Value format: "<rule_id>:<cluster_id>" — keep it compact (Slack caps
    # button values at 2000 chars).
    try:
        rule_id_str, cluster_id = value.split(":", 1)
        rule_id = int(rule_id_str)
    except (ValueError, AttributeError):
        return _resp(
            200,
            {"response_type": "ephemeral", "text": ":warning: malformed action value"},
        )

    user = (payload.get("user") or {})
    user_name = user.get("username") or user.get("name") or user.get("id") or "unknown"

    try:
        rows = _execute(
            "UPDATE alert_rules SET last_acked_at = NOW(), last_acked_by = :user "
            "WHERE id = :id RETURNING id, name, cluster_id, last_acked_at::text AS acked_at",
            {"user": user_name, "id": rule_id},
        )
        if not rows:
            return _resp(
                200,
                {
                    "response_type": "ephemeral",
                    "text": f":warning: rule {rule_id} not found",
                },
            )
        rule = rows[0]
        # Audit trail — keep the original alert + the ack in one queryable
        # place. event_log is the same sink the alert_evaluator writes to.
        _execute(
            "INSERT INTO event_log (cluster_id, event_time, event_type, source, "
            "severity, message, raw_event) "
            "VALUES (:cid, NOW(), 'alert_ack', 'dbops-slack-interactive', 'info', "
            ":msg, :raw::jsonb)",
            {
                "cid": cluster_id,
                "msg": f"Rule {rule_id} acked by @{user_name} via Slack",
                "raw": json.dumps(
                    {
                        "rule_id": rule_id,
                        "rule_name": rule.get("name"),
                        "user": user_name,
                        "channel": (payload.get("channel") or {}).get("name"),
                        "team": (payload.get("team") or {}).get("domain"),
                    }
                ),
            },
        )
    except Exception:
        print(f"Slack ack DB error: {traceback.format_exc()}")
        return _resp(
            200,
            {
                "response_type": "ephemeral",
                "text": ":warning: DBOps could not persist the ack — see CloudWatch logs",
            },
        )

    return _resp(
        200,
        _acked_blocks(
            rule_name=str(rule.get("name") or ""),
            cluster_id=str(rule.get("cluster_id") or cluster_id),
            rule_id=rule_id,
            user_display=user_name,
            when_iso=str(rule.get("acked_at") or ""),
        ),
    )
