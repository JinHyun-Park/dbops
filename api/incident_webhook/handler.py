"""Inbound incident webhook — Datadog / PagerDuty → event_log (P4).

External monitors POST an incident here; we authenticate with a shared secret,
extract (cluster, title, severity, link), and write an `external_incident` row
to event_log. That row surfaces in the dashboard Events panel, where each
external incident gets a one-click "Chat에서 진단" deep-link (the deep-link
inbox model — a DBA starts the agent RCA; we never auto-run the agent).

  POST /api/incident-webhook   (public route; authenticated by shared secret)

Auth: a static shared secret in the `X-DBOps-Webhook-Token` header, compared in
constant time against INCIDENT_WEBHOOK_SECRET. Datadog custom webhooks and
PagerDuty both let you attach a static custom header, so this works for both
without a signing proxy. Fail-closed: no secret configured → 503; bad/missing
token → 401. (Body-HMAC would be stronger but Datadog can't HMAC its payload.)
"""
import base64
import hmac
import json
import os

import boto3

_SECRET_ENV = "INCIDENT_WEBHOOK_SECRET"
_TOKEN_HEADER = "x-dbops-webhook-token"


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str),
    }


def _raw_body(event: dict) -> str:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except Exception:
            return ""
    return body


def _verify(headers: dict) -> tuple[bool, str]:
    secret = os.environ.get(_SECRET_ENV)
    if not secret:
        return False, "INCIDENT_WEBHOOK_SECRET not configured on this deployment"
    norm = {k.lower(): v for k, v in (headers or {}).items()}
    token = norm.get(_TOKEN_HEADER, "")
    if not token:
        return False, "missing X-DBOps-Webhook-Token"
    if not hmac.compare_digest(token, secret):
        return False, "token mismatch"
    return True, ""


_DD_SEV = {"error": "critical", "alert": "critical", "warning": "warning"}
_PD_SEV = {"high": "critical", "low": "warning"}


def _cluster_from_tags(tags) -> str:
    """Datadog tags arrive as a CSV string or a list ('cluster:foo,env:prod').
    Pull the cluster identifier from common tag keys."""
    if isinstance(tags, str):
        tags = tags.split(",")
    if not isinstance(tags, list):
        return ""
    for raw in tags:
        t = str(raw).strip()
        for key in ("cluster:", "dbcluster:", "db_cluster:", "cluster_id:"):
            if t.lower().startswith(key):
                return t[len(key):].strip()
    return ""


def parse_incident(payload: dict) -> dict:
    """Normalize a Datadog OR PagerDuty (OR generic) payload to a common shape.
    Tolerant: every field falls back to a safe default so a malformed payload
    still produces a usable event_log row rather than 500-ing."""
    src = "external"
    title = severity = url = cluster_id = ""

    # PagerDuty v3: { "event": { "data": { ... } } }
    pd = (payload.get("event") or {}).get("data") if isinstance(payload.get("event"), dict) else None
    if isinstance(pd, dict):
        src = "pagerduty"
        title = pd.get("title") or ""
        url = pd.get("html_url") or ""
        severity = _PD_SEV.get(str(pd.get("urgency", "")).lower(), "warning")
        cd = pd.get("custom_details") if isinstance(pd.get("custom_details"), dict) else {}
        cluster_id = (
            cd.get("cluster_id")
            or cd.get("cluster")
            or (pd.get("service") or {}).get("summary", "")
            if isinstance(cd, dict)
            else ""
        ) or ""
    else:
        # Datadog custom webhook (user-templated, flat) or generic JSON.
        src = "datadog" if ("alert_type" in payload or "alert_id" in payload) else "external"
        title = payload.get("title") or payload.get("alert_title") or payload.get("message") or ""
        url = payload.get("link") or payload.get("url") or ""
        severity = _DD_SEV.get(
            str(payload.get("alert_type") or payload.get("priority") or "").lower(),
            "info",
        )
        cluster_id = (
            str(payload.get("cluster_id") or payload.get("cluster") or "").strip()
            or _cluster_from_tags(payload.get("tags"))
        )

    return {
        "source": src,
        "title": (title or "external incident")[:500],
        "severity": severity or "info",
        "url": url[:1000] if isinstance(url, str) else "",
        "cluster_id": (cluster_id or "")[:255],
    }


def _write_event(inc: dict) -> None:
    raw = json.dumps(
        {"title": inc["title"], "url": inc["url"], "source": inc["source"]},
        ensure_ascii=False,
    )
    params = [
        {"name": "cluster_id", "value": {"stringValue": inc["cluster_id"] or "unknown"}},
        {"name": "source", "value": {"stringValue": inc["source"]}},
        {"name": "severity", "value": {"stringValue": inc["severity"]}},
        {"name": "message", "value": {"stringValue": inc["title"]}},
        {"name": "raw_event", "value": {"stringValue": raw}},
    ]
    boto3.client("rds-data").execute_statement(
        resourceArn=os.environ["CACHE_DB_CLUSTER_ARN"],
        secretArn=os.environ["CACHE_DB_SECRET_ARN"],
        database=os.environ.get("CACHE_DB_NAME", "dbops"),
        sql=(
            "/* source=dbops-incident-webhook */ "
            "INSERT INTO event_log (cluster_id, event_time, event_type, source, "
            "severity, message, raw_event) VALUES (:cluster_id, now(), "
            "'external_incident', :source, :severity, :message, "
            "CAST(:raw_event AS jsonb))"
        ),
        parameters=params,
    )


def lambda_handler(event, context):
    ok, why = _verify(event.get("headers") or {})
    if not ok:
        # 503 when the deployment hasn't configured the secret; 401 otherwise.
        status = 503 if "not configured" in why else 401
        return _resp(status, {"error": why})
    try:
        payload = json.loads(_raw_body(event) or "{}")
        if not isinstance(payload, dict):
            return _resp(400, {"error": "expected a JSON object"})
    except json.JSONDecodeError:
        return _resp(400, {"error": "invalid JSON body"})

    inc = parse_incident(payload)
    try:
        _write_event(inc)
    except Exception as e:
        print(f"[incident-webhook] event_log write failed: {type(e).__name__}")
        return _resp(500, {"error": "failed to record incident"})
    # Best-effort instant in-app push (no-op when the WS channel isn't configured).
    try:
        from ws_notify import broadcast

        broadcast({
            "type": "incident",
            "source": inc["source"],
            "cluster_id": inc["cluster_id"],
            "severity": inc["severity"],
            "title": inc["title"],
        })
    except Exception as e:
        print(f"[incident-webhook] ws broadcast failed: {type(e).__name__}")
    return _resp(
        200,
        {
            "status": "recorded",
            "source": inc["source"],
            "cluster_id": inc["cluster_id"],
            "severity": inc["severity"],
        },
    )
