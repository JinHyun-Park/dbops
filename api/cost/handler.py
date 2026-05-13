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
import boto3
from datetime import datetime, timedelta


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

    # Two parallel queries: tagged vs untagged. Both scoped to Bedrock-family
    # SERVICE values so we don't include unrelated AWS spend.
    tagged_daily, tagged_total, tagged_err = _query_total(ce, start, end, services, tag_filter)
    all_daily, all_total, all_err = _query_total(ce, start, end, services, None)

    # If tag filter returns 0 but there IS Bedrock spend, the user needs to
    # either activate the Application tag in Billing or add it to their
    # Bedrock resources (AgentCore Runtime, Application Inference Profile,
    # etc.). Surface this clearly instead of silently showing $0.
    tag_warning = None
    if tagged_total == 0 and all_total > 0:
        tag_warning = (
            "Tag-attributed Bedrock spend is $0 but the account shows "
            f"${all_total:.2f} of Bedrock spend in this window. Either the "
            "'Application' cost allocation tag is not activated in the AWS "
            "Billing console (Cost allocation tags page — requires "
            "management-account permissions in an AWS Organizations setup), "
            "or your Bedrock-using resources are not tagged with "
            "Application=DBOps. The chart below shows the untagged total so "
            "you still see something — activate the tag for accurate "
            "attribution."
        )

    # The headline number is the tagged total when it's nonzero; otherwise
    # we fall back to the untagged Bedrock-family total so the UI doesn't
    # show $0 while the account is clearly spending on Bedrock.
    headline_total = tagged_total if tagged_total > 0 else all_total
    headline_daily = tagged_daily if tagged_total > 0 else all_daily

    # Per-model breakdown by USAGE_TYPE (e.g., "APN1-Bedrock:Tokens:Input:Anthropic:Claude-Sonnet-4-6").
    model_split = []
    try:
        breakdown_filter = (
            {"And": [{"Dimensions": {"Key": "SERVICE", "Values": services}}, tag_filter]}
            if tagged_total > 0
            else {"Dimensions": {"Key": "SERVICE", "Values": services}}
        )
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
    if all_total == 0:
        no_data_reason = (
            "No Bedrock spend in this window — either you haven't invoked "
            "Bedrock yet, or Cost Explorer hasn't caught up (24-48h lag)."
        )
    elif tagged_err == "cost_allocation_tag_not_activated":
        no_data_reason = (
            "The 'Application' cost allocation tag is not activated in the "
            "AWS Billing console — activate it, then re-check in 24h."
        )

    return _response(200, {
        "env": _ENV,
        "range_days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total": round(headline_total, 4),
        "total_tagged": round(tagged_total, 4),
        "total_all_bedrock": round(all_total, 4),
        "currency": "USD",
        "daily": headline_daily,
        "by_usage_type": model_split,
        "no_data_reason": no_data_reason,
        "tag_warning": tag_warning,
        "discovered_services": services,
    })
