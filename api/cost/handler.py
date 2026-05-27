"""Bedrock cost dashboard data — uses Cost Explorer GetCostAndUsage to surface
Application=DBOps tagged spend by day and by Claude model.

NOTE: Cost Explorer is global, billed per request (~$0.01), and only returns
fresh data after the next billing cycle (≈24h lag). The frontend caches results
for at least 1h so the API isn't hammered.

AWS recently split Bedrock model billing into per-model SERVICE entries
(e.g., "Claude Sonnet 4.6 (Amazon Bedrock Edition)") alongside the legacy
"Amazon Bedrock" name and "Amazon Bedrock AgentCore". A single hardcoded
SERVICE filter misses everything, so we discover Bedrock-family SERVICE
values at runtime via GetDimensionValues and feed that list into all
cost queries. Discovery is cheap (one CE call) and the response is cached
per Lambda invocation.
"""

import json
import os
from datetime import datetime, timedelta

import boto3

_ENV = os.environ.get("ENV", "dev")


def _cors():
    return {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}


def _response(status, body):
    return {"statusCode": status, "headers": _cors(), "body": json.dumps(body, default=str)}


def _bedrock_services(ce, start, end):
    """Enumerate SERVICE dimension values that look like Bedrock. AWS adds new
    per-model entries over time (`Claude X.Y (Amazon Bedrock Edition)`) so a
    hardcoded list goes stale. Falls back to a sensible default if discovery
    fails."""
    DEFAULT = [
        "Amazon Bedrock",
        "Amazon Bedrock AgentCore",
    ]
    keep = []
    try:
        resp = ce.get_dimension_values(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Dimension="SERVICE",
        )
        for v in resp.get("DimensionValues", []):
            name = v.get("Value", "")
            low = name.lower()
            if "bedrock" in low or ("claude" in low and "bedrock" in low):
                keep.append(name)
    except Exception as e:
        print(f"GetDimensionValues failed: {e}")
    if not keep:
        return DEFAULT
    return keep


def _query_total(ce, start, end, services, tag_filter=None):
    """Total daily spend for the given services, optionally tag-filtered.
    Returns (daily_list, total_amount, error_string_or_none)."""
    service_filter = {"Dimensions": {"Key": "SERVICE", "Values": services}}
    cost_filter = (
        {"And": [service_filter, tag_filter]} if tag_filter else service_filter
    )
    daily = []
    total = 0.0
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            Filter=cost_filter,
        )
        for r in resp.get("ResultsByTime", []):
            amount = float(r["Total"]["UnblendedCost"]["Amount"])
            daily.append({"date": r["TimePeriod"]["Start"], "amount": amount})
            total += amount
        return daily, total, None
    except Exception as e:
        msg = str(e).lower()
        if "is not currently activated" in msg or "data is not available" in msg or "not activated" in msg:
            return daily, total, "cost_allocation_tag_not_activated"
        return daily, total, str(e)[:200]


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method") \
        or event.get("httpMethod", "GET")
    if method != "GET":
        return _response(405, {"error": f"method {method} not allowed"})

    qs = event.get("queryStringParameters") or {}
    days = max(7, min(int(qs.get("days") or "30"), 90))

    ce = boto3.client("ce", region_name="us-east-1")  # CE is global; us-east-1 is the standard endpoint.
    end = datetime.utcnow().date()
    start = end - timedelta(days=days)

    services = _bedrock_services(ce, start, end)

    tag_filter = {"Tags": {"Key": "Application", "Values": ["DBOps"]}}

    # Single Cost Explorer query scoped to BOTH the Bedrock-family SERVICE
    # values AND the Application=DBOps tag. We deliberately don't compute an
    # untagged "account-wide Bedrock" total — the account may host other
    # projects' Bedrock workloads (Kiro, ad-hoc experiments) that have nothing
    # to do with DBOps, so mixing them in would misattribute spend.
    tagged_daily, tagged_total, tagged_err = _query_total(ce, start, end, services, tag_filter)

    # When tagged_total is 0, the most likely cause is that the user has not
    # yet activated `Application` as a cost-allocation tag in AWS Billing —
    # CDK already stamps the tag on every AIP/Lambda/RDS resource, but Cost
    # Explorer ignores tags until they're explicitly activated, and activation
    # does NOT back-fill past spend. Surface a one-time activation guide.
    tag_warning = None
    if tagged_total == 0:
        tag_warning = (
            "DBOps Bedrock calls are routed through tagged Application "
            "Inference Profiles, but the 'Application' cost allocation tag "
            "is not yet activated in the AWS Billing console (or you haven't "
            "used Bedrock yet in this window). Activate the tag once — Cost "
            "Explorer starts attributing DBOps spend within ~24h. "
            "(Note: activation does not back-fill past spend.)"
        )

    headline_total = tagged_total
    headline_daily = tagged_daily

    # Per-model breakdown by USAGE_TYPE — scoped to the same Bedrock+tag
    # filter so the table only shows DBOps spend.
    model_split = []
    try:
        breakdown_filter = {
            "And": [{"Dimensions": {"Key": "SERVICE", "Values": services}}, tag_filter]
        }
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            Filter=breakdown_filter,
        )
        rollup = {}
        for r in resp.get("ResultsByTime", []):
            for g in r.get("Groups", []):
                ut = g["Keys"][0]
                amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
                qty = float(g["Metrics"]["UsageQuantity"]["Amount"])
                cur = rollup.setdefault(ut, {"usage_type": ut, "amount": 0.0, "quantity": 0.0})
                cur["amount"] += amount
                cur["quantity"] += qty
        model_split = sorted(rollup.values(), key=lambda x: x["amount"], reverse=True)
    except Exception as e:
        print(f"model_split error: {e}")

    no_data_reason = None
    if tagged_err == "cost_allocation_tag_not_activated":
        no_data_reason = (
            "The 'Application' cost allocation tag is not activated in the "
            "AWS Billing console — activate it, then re-check in 24h."
        )

    anomalies = _detect_anomalies(headline_daily)

    return _response(200, {
        "env": _ENV,
        "range_days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total": round(headline_total, 4),
        "total_tagged": round(tagged_total, 4),
        "currency": "USD",
        "daily": headline_daily,
        "by_usage_type": model_split,
        "anomalies": anomalies,
        "no_data_reason": no_data_reason,
        "tag_warning": tag_warning,
        "discovered_services": services,
    })


def _detect_anomalies(daily: list[dict]) -> list[dict]:
    """Walk the daily series and flag spikes vs a trailing 7-day baseline.

    Rule (intentionally permissive on small absolute values — finance teams
    don't care about a $0.10 → $0.30 jump):

      baseline = mean of the 7 days *preceding* this day
      stddev   = population stddev over the same 7 days
      z_score  = (x - mean) / max(stddev, mean * 0.15)
      flag if  z_score > 2.0 AND (x > mean * 1.5) AND (x - mean) > 0.5

    severity:
      - critical : z_score >= 3.5 or (x > mean * 3.0 AND x - mean > 2.0)
      - warning  : otherwise

    Returns most-recent-first so the UI can highlight today/yesterday."""
    import math

    if len(daily) < 8:
        # Need at least 7-day baseline + 1 day to evaluate.
        return []

    out: list[dict] = []
    series = [(d.get("date"), float(d.get("amount") or 0)) for d in daily]
    for i in range(7, len(series)):
        date, amount = series[i]
        window = [v for _, v in series[i - 7 : i]]
        mean = sum(window) / 7.0
        if mean <= 0 and amount <= 0:
            continue
        variance = sum((v - mean) ** 2 for v in window) / 7.0
        stddev = math.sqrt(variance)
        denom = max(stddev, mean * 0.15, 0.01)
        z = (amount - mean) / denom
        delta_pct = ((amount - mean) / mean * 100.0) if mean > 0 else None

        threshold_z = 2.0
        meets_z = z > threshold_z
        meets_relative = amount > mean * 1.5 and (amount - mean) > 0.5
        if not (meets_z and meets_relative):
            continue

        severity = (
            "critical"
            if z >= 3.5 or (mean > 0 and amount > mean * 3.0 and amount - mean > 2.0)
            else "warning"
        )
        out.append(
            {
                "date": date,
                "amount": round(amount, 4),
                "baseline_mean": round(mean, 4),
                "baseline_stddev": round(stddev, 4),
                "z_score": round(z, 2),
                "delta_pct": round(delta_pct, 1) if delta_pct is not None else None,
                "severity": severity,
            }
        )

    # Most recent first — the panel shows newest first so today/yesterday
    # spikes are immediately visible without scrolling.
    out.sort(key=lambda a: a["date"], reverse=True)
    return out
