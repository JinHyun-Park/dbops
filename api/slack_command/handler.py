"""Slack slash command endpoint — `/dbops <subcommand> [args]`.

Lets the on-call DBA hit DBOps from inside Slack without context-
switching to the web console. Mirror of slack_interactive's HMAC v0
verification + 5-min replay window.

Supported subcommands (parsed from the `text` field):
  status <cluster>     — connection + ETL freshness + last alert
  timeline <cluster>   — deep-link to /timeline?cluster=<id>
  clusters             — list registered clusters
  help                 — usage panel

Configuration in Slack app:
  Request URL: <api-gateway>/api/slack/command (POST)
  Command:     /dbops
  Description: DBOps console shortcuts

The signing secret env var is shared with slack_interactive
(SLACK_SIGNING_SECRET) so workspaces don't need a second secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse

import boto3

_SIGNING_SECRET_ENV = "SLACK_SIGNING_SECRET"
_MAX_REQUEST_AGE_S = 60 * 5


def _verify_slack_signature(headers: dict, raw_body: str) -> tuple[bool, str]:
    """Slack v0 HMAC verification. Same shape as slack_interactive —
    duplicated rather than imported to avoid a Lambda layer just for
    two functions."""
    secret = os.environ.get(_SIGNING_SECRET_ENV)
    if not secret:
        return False, "SLACK_SIGNING_SECRET not configured on this deployment"
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


def _slack_text(text: str) -> dict:
    """Plain-text Slack response. Use ephemeral so only the invoker
    sees the reply (channel doesn't get spammed during incident triage)."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"response_type": "ephemeral", "text": text}),
    }


def _slack_blocks(blocks: list) -> dict:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"response_type": "ephemeral", "blocks": blocks}),
    }


def lambda_handler(event, context):
    raw_body = _get_raw_body(event)
    ok, reason = _verify_slack_signature(event.get("headers") or {}, raw_body)
    if not ok:
        # Return 200 with an error message so Slack doesn't retry, but
        # the user sees what went wrong.
        return _slack_text(f"⚠ verification failed: {reason}")

    parsed = urllib.parse.parse_qs(raw_body)
    text = (parsed.get("text") or [""])[0].strip()
    user_name = (parsed.get("user_name") or [""])[0]

    if not text or text in ("help", "?"):
        return _slack_blocks(_help_block(user_name))

    parts = text.split()
    sub = parts[0].lower()
    args = parts[1:]

    if sub == "status":
        return _cmd_status(args)
    if sub == "timeline":
        return _cmd_timeline(args)
    if sub in ("clusters", "list"):
        return _cmd_clusters()
    return _slack_text(
        f"❓ Unknown subcommand `{sub}`. Try `/dbops help`."
    )


def _frontend_url() -> str:
    return os.environ.get("FRONTEND_URL", "").rstrip("/")


def _help_block(user_name: str) -> list:
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "DBOps /dbops"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"hi {user_name or 'there'} — DBOps shortcuts:\n"
                    "• `/dbops status <cluster>` — connection + ETL freshness\n"
                    "• `/dbops timeline <cluster>` — incident context deep-link\n"
                    "• `/dbops clusters` — registered clusters\n"
                    "• `/dbops help` — this message"
                ),
            },
        },
    ]


def _cmd_status(args: list[str]) -> dict:
    if not args:
        return _slack_text("Usage: `/dbops status <cluster_id>`")
    cluster_id = args[0]
    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not table_name:
        return _slack_text("⚠ CLUSTERS_TABLE not configured")
    try:
        ddb = boto3.resource("dynamodb").Table(table_name)
        item = ddb.get_item(Key={"cluster_id": cluster_id}).get("Item")
    except Exception as e:
        # Unauthenticated (signature-verified only) webhook route: a DynamoDB /
        # STS error message here would post the hub account id and the platform
        # role name into a Slack workspace. Static text, detail to CloudWatch.
        print(f"[slack_command] status lookup failed for {cluster_id}: {e}")
        return _slack_text(
            f"⚠ `{cluster_id}` 조회에 실패했습니다 (클러스터 레지스트리 접근 오류). "
            "자세한 원인은 서버 로그를 확인하세요."
        )
    if not item:
        return _slack_text(
            f"❓ `{cluster_id}` not registered. Try `/dbops clusters`."
        )
    conn = item.get("connection_status") or "untested"
    conn_emoji = {"ok": "🟢", "failed": "🔴", "untested": "⚪"}.get(conn, "⚪")
    region = item.get("region", "—")
    engine = item.get("engine", "—")
    fe = _frontend_url()
    lines = [
        f"*{cluster_id}*  ({engine}, {region})",
        f"{conn_emoji} connection: `{conn}`",
    ]
    if item.get("connection_error"):
        lines.append(f"  └─ {item['connection_error']}")
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        }
    ]
    if fe:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🕐 Timeline"},
                    "url": f"{fe}/timeline?cluster={urllib.parse.quote(cluster_id)}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open dashboard"},
                    "url": f"{fe}/dashboard?cluster={urllib.parse.quote(cluster_id)}",
                },
            ],
        })
    return _slack_blocks(blocks)


def _cmd_timeline(args: list[str]) -> dict:
    if not args:
        return _slack_text("Usage: `/dbops timeline <cluster_id>`")
    cluster_id = args[0]
    fe = _frontend_url()
    if not fe:
        return _slack_text(
            "⚠ FRONTEND_URL is not configured — set it on the agent stack."
        )
    url = f"{fe}/timeline?cluster={urllib.parse.quote(cluster_id)}"
    return _slack_blocks([
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Timeline for `{cluster_id}`* — 알람·RDS 이벤트·스키마 "
                    "변경·실행된 쓰기가 시간순으로:"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🕐 Open timeline"},
                    "url": url,
                    "style": "primary",
                }
            ],
        },
    ])


def _cmd_clusters() -> dict:
    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not table_name:
        return _slack_text("⚠ CLUSTERS_TABLE not configured")
    try:
        ddb = boto3.resource("dynamodb").Table(table_name)
        resp = ddb.scan(ProjectionExpression="cluster_id, engine, connection_status")
        items = resp.get("Items", [])
    except Exception as e:
        print(f"[slack_command] clusters scan failed: {e}")
        return _slack_text(
            "⚠ 클러스터 목록 조회에 실패했습니다 (클러스터 레지스트리 접근 오류). "
            "자세한 원인은 서버 로그를 확인하세요."
        )
    if not items:
        return _slack_text("(no clusters registered)")
    lines = []
    for it in sorted(items, key=lambda x: x.get("cluster_id", ""))[:50]:
        conn = it.get("connection_status") or "untested"
        emoji = {"ok": "🟢", "failed": "🔴", "untested": "⚪"}.get(conn, "⚪")
        lines.append(
            f"{emoji} `{it.get('cluster_id')}` · {it.get('engine', '—')}"
        )
    return _slack_blocks([
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Registered clusters ({len(items)}):*\n" + "\n".join(lines),
            },
        }
    ])
