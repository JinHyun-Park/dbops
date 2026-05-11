"""Bedrock cost dashboard data — uses Cost Explorer GetCostAndUsage to surface
Application=DBOps tagged spend by day and by Claude model.

NOTE: Cost Explorer is global, billed per request (~$0.01), and only returns
fresh data after the next billing cycle (≈24h lag). The frontend caches results
for at least 1h so the API isn't hammered.
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

    tag_filter = {
        "Tags": {
            "Key": "Application",
            "Values": ["DBOps"],
        }
    }

    daily = []
    model_split = []
    total = 0.0
    no_data_reason = None

    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            Filter=tag_filter,
        )
        for r in resp.get("ResultsByTime", []):
            amount = float(r["Total"]["UnblendedCost"]["Amount"])
            daily.append({
                "date": r["TimePeriod"]["Start"],
                "amount": amount,
            })
            total += amount
    except ce.exceptions.DataUnavailableException as e:
        no_data_reason = "no tagged spend yet — activate the 'Application' cost allocation tag in the Billing console (24h propagation), then wait one billing cycle"
    except Exception as e:
        msg = str(e).lower()
        if "is not currently activated" in msg or "data is not available" in msg or "not activated" in msg:
            no_data_reason = "cost allocation tag 'Application' is not activated in the Billing console — activate it, then re-check in 24h"
        else:
            return _response(500, {"error": str(e)[:300]})

    # Per-model breakdown by USAGE_TYPE (e.g., "APN1-Bedrock:Tokens:Input:Anthropic:Claude-Sonnet-4-6")
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
            Filter={
                "And": [
                    tag_filter,
                    {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Bedrock"]}},
                ]
            },
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

    return _response(200, {
        "env": _ENV,
        "range_days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total": round(total, 4),
        "currency": "USD",
        "daily": daily,
        "by_usage_type": model_split,
        "no_data_reason": no_data_reason,
    })
