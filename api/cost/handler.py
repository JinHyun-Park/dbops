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
from datetime import datetime, timedelta, timezone

import boto3
import tenancy

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
        # The error string this returns is a BOUNDED token the callers branch on,
        # never the exception message: CE/STS text carries the hub account id, the
        # platform role name and ARNs, and `no_data_reason` is browser-facing.
        print(f"[cost] get_cost_and_usage (total) failed: {e}")
        msg = str(e).lower()
        if "is not currently activated" in msg or "data is not available" in msg or "not activated" in msg:
            return daily, total, "cost_allocation_tag_not_activated"
        return daily, total, "cost_explorer_query_failed"


# ===========================================================================
# RDS / Aurora cost path (?view=rds)
# ---------------------------------------------------------------------------
# DBAs want "이 Aurora 클러스터가 한 달에 얼마지?". Unlike Bedrock — where DBOps
# routes everything through Application Inference Profiles it controls — RDS/
# Aurora resources are the *customer's own clusters*, which DBOps does not tag
# or own. So per-cluster attribution is only possible if the operator has
# activated a cost-allocation tag (e.g. `dbops:cluster`) on their clusters.
# We attempt that grouping and, when it returns nothing, surface a clear
# `per_cluster_available: false` flag + activation note rather than inventing
# per-cluster numbers. The always-available view is RDS total + a usage-type
# breakdown (Aurora I/O, storage, instance hours, backup, etc.).
# ===========================================================================

# Canonical RDS-family SERVICE name + Aurora alias. AWS bills Aurora under the
# "Amazon Relational Database Service" SERVICE dimension; "Amazon Aurora" can
# appear as a separate entry in some accounts/regions, so we keep both as a
# fallback when discovery fails.
_RDS_SERVICE_DEFAULT = [
    "Amazon Relational Database Service",
    "Amazon Aurora",
]

# Cost-allocation tag keys that plausibly identify an Aurora cluster. CE
# stores user-defined tags un-prefixed (no "user:" needed when passed via the
# Tags filter Key). We try each until one yields grouped rows.
_CLUSTER_TAG_CANDIDATES = ["dbops:cluster", "cluster", "ClusterId", "DBClusterIdentifier"]


def _rds_services(ce, start, end):
    """Enumerate SERVICE dimension values that look like RDS/Aurora. Mirrors
    `_bedrock_services` so the breakdown stays correct even if AWS renames or
    splits the RDS service entry. Falls back to canonical names on failure."""
    keep = []
    try:
        resp = ce.get_dimension_values(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Dimension="SERVICE",
        )
        for v in resp.get("DimensionValues", []):
            name = v.get("Value", "")
            low = name.lower()
            if "relational database" in low or "rds" in low or "aurora" in low:
                keep.append(name)
    except Exception as e:
        print(f"GetDimensionValues (RDS) failed: {e}")
    if not keep:
        return list(_RDS_SERVICE_DEFAULT)
    return keep


_ELASTICACHE_SERVICE_DEFAULT = ("Amazon ElastiCache",)


def _elasticache_services(ce, start, end):
    """SERVICE dimension values that look like ElastiCache. Mirrors _rds_services."""
    keep = []
    try:
        resp = ce.get_dimension_values(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Dimension="SERVICE",
        )
        for v in resp.get("DimensionValues", []):
            name = v.get("Value", "")
            if "elasticache" in name.lower():
                keep.append(name)
    except Exception as e:
        print(f"GetDimensionValues (ElastiCache) failed: {e}")
    if not keep:
        return list(_ELASTICACHE_SERVICE_DEFAULT)
    return keep


def _query_by_dimension(ce, start, end, services, dimension):
    """Roll up cost + usage by a CE DIMENSION (USAGE_TYPE / INSTANCE_TYPE)
    scoped to the RDS-family SERVICE values. Returns (rows, error_or_none)
    where each row is {usage_type, amount, quantity}."""
    rollup = {}
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[{"Type": "DIMENSION", "Key": dimension}],
            Filter={"Dimensions": {"Key": "SERVICE", "Values": services}},
        )
        for r in resp.get("ResultsByTime", []):
            for g in r.get("Groups", []):
                key = g["Keys"][0]
                amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
                qty = float(g["Metrics"]["UsageQuantity"]["Amount"])
                cur = rollup.setdefault(key, {"usage_type": key, "amount": 0.0, "quantity": 0.0})
                cur["amount"] += amount
                cur["quantity"] += qty
        rows = sorted(rollup.values(), key=lambda x: x["amount"], reverse=True)
        return rows, None
    except Exception as e:
        print(f"[cost] get_cost_and_usage (by {dimension}) failed: {e}")
        return [], "cost_explorer_query_failed"


def _query_per_cluster(ce, start, end, services):
    """Attempt per-cluster attribution by grouping RDS spend on a cost-
    allocation TAG. Tries each candidate tag key; the first that returns
    non-empty grouped rows wins.

    Returns (rows, tag_key, error_or_none):
      - rows: [{cluster, amount}] sorted desc (empty list if no tag yields data)
      - tag_key: the tag key that produced rows, else None
      - error_or_none: "cost_allocation_tag_not_activated" when CE rejects the
        tag because it isn't activated, else a short message, else None.

    Per-cluster requires the operator to have activated the tag in AWS Billing
    AND tagged their clusters with it — neither is something DBOps can do for
    customer-owned RDS resources. We never fabricate rows; an empty result
    means "not available", surfaced as a flag to the caller."""
    last_err = None
    for tag_key in _CLUSTER_TAG_CANDIDATES:
        rollup = {}
        try:
            resp = ce.get_cost_and_usage(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                Granularity="DAILY",
                Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "TAG", "Key": tag_key}],
                Filter={"Dimensions": {"Key": "SERVICE", "Values": services}},
            )
        except Exception as e:
            print(f"[cost] per-cluster grouping on tag {tag_key} failed: {e}")
            msg = str(e).lower()
            if "is not currently activated" in msg or "not activated" in msg:
                last_err = "cost_allocation_tag_not_activated"
            else:
                last_err = "cost_explorer_query_failed"
            continue
        for r in resp.get("ResultsByTime", []):
            for g in r.get("Groups", []):
                # TAG groups come back as "tag_key$value" (empty value = untagged).
                raw = g["Keys"][0]
                value = raw.split("$", 1)[1] if "$" in raw else raw
                if not value:
                    continue  # skip the untagged bucket — not a real cluster
                amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
                rollup[value] = rollup.get(value, 0.0) + amount
        rows = [
            {"cluster": k, "amount": round(v, 4)}
            for k, v in rollup.items()
            if v != 0.0
        ]
        if rows:
            rows.sort(key=lambda x: x["amount"], reverse=True)
            return rows, tag_key, None
    return [], None, last_err


def _handle_rds_view(ce, start, end, days, event=None):
    """Build the RDS/Aurora cost response. Same envelope conventions as the
    Bedrock path (total, currency, daily, by_usage_type, anomalies, ...) plus
    RDS-specific per-cluster fields."""
    services = _rds_services(ce, start, end)

    # No tag filter — RDS spend is the customer's own clusters; DBOps doesn't
    # tag them. We report the whole account's RDS/Aurora bill.
    daily, total, total_err = _query_total(ce, start, end, services)

    by_usage_type, _ut_err = _query_by_dimension(ce, start, end, services, "USAGE_TYPE")

    per_cluster, cluster_tag, cluster_err = _query_per_cluster(ce, start, end, services)

    # Tenant filter: restrict per-cluster rows to the caller's visible clusters.
    # Totals and by_usage_type are account-level aggregates (not per-cluster) —
    # intentionally left unfiltered; only the per-cluster breakdown is scoped.
    if event is not None:
        visible = tenancy.visible_set_from_registry(event)
        if visible is not None:
            per_cluster = [r for r in per_cluster if r.get("cluster") in visible]

    per_cluster_available = len(per_cluster) > 0

    per_cluster_note = None
    if not per_cluster_available:
        per_cluster_note = (
            "클러스터별 비용 분리를 사용할 수 없습니다. AWS Billing 콘솔에서 "
            "cost-allocation 태그(예: 'dbops:cluster')를 활성화하고 Aurora "
            "클러스터에 적용하면 약 24시간 내에 Cost Explorer가 클러스터 단위로 "
            "비용을 분리합니다. 리소스 수준 CE 데이터는 추가 비용이 발생해 "
            "사용하지 않으며, 과거 비용은 소급 반영되지 않습니다."
        )

    no_data_reason = None
    if total_err == "cost_allocation_tag_not_activated":
        # Shouldn't happen without a tag filter, but guard anyway.
        no_data_reason = (
            "Cost Explorer가 RDS 데이터를 반환하지 않았습니다 — 이 계정에서 "
            "Cost Explorer가 활성화되어 있는지 확인 후 24시간 뒤 다시 확인하세요."
        )
    elif total == 0 and not daily:
        no_data_reason = (
            "이 기간에 기록된 RDS/Aurora 비용이 없습니다. Aurora를 운영 중이라면 "
            "Cost Explorer 활성화 여부를 확인하세요 (반영까지 약 24시간 지연)."
        )

    anomalies = _detect_anomalies(daily)

    return _response(200, {
        "env": _ENV,
        "view": "rds",
        "range_days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total": round(total, 4),
        "currency": "USD",
        "daily": daily,
        "by_usage_type": by_usage_type,
        "per_cluster": per_cluster,
        "per_cluster_available": per_cluster_available,
        "per_cluster_tag": cluster_tag,
        "per_cluster_note": per_cluster_note,
        "anomalies": anomalies,
        "no_data_reason": no_data_reason,
        "discovered_services": services,
    })


def _handle_elasticache_view(ce, start, end, days, event=None):
    """ElastiCache spend (Cost Explorer). Same envelope as the RDS view."""
    services = _elasticache_services(ce, start, end)
    daily, total, total_err = _query_total(ce, start, end, services)
    by_usage_type, _ut_err = _query_by_dimension(ce, start, end, services, "USAGE_TYPE")
    per_cluster, cluster_tag, cluster_err = _query_per_cluster(ce, start, end, services)

    # Tenant filter: restrict per-cluster rows to the caller's visible clusters.
    # Totals and by_usage_type are account-level aggregates — intentionally
    # left unfiltered; only the per-cluster breakdown is tenant-scoped.
    if event is not None:
        visible = tenancy.visible_set_from_registry(event)
        if visible is not None:
            per_cluster = [r for r in per_cluster if r.get("cluster") in visible]

    per_cluster_available = len(per_cluster) > 0
    per_cluster_note = None
    if not per_cluster_available:
        per_cluster_note = (
            "클러스터별 비용 분리를 사용할 수 없습니다. AWS Billing 콘솔에서 "
            "cost-allocation 태그를 활성화하고 ElastiCache 클러스터에 적용하면 약 "
            "24시간 내에 Cost Explorer가 클러스터 단위로 비용을 분리합니다. 과거 "
            "비용은 소급 반영되지 않습니다."
        )
    no_data_reason = None
    if total == 0 and not daily:
        no_data_reason = (
            "이 기간에 기록된 ElastiCache 비용이 없습니다. ElastiCache를 운영 중이라면 "
            "Cost Explorer 활성화 여부를 확인하세요 (반영까지 약 24시간 지연)."
        )
    anomalies = _detect_anomalies(daily)
    return _response(200, {
        "env": _ENV,
        "view": "elasticache",
        "range_days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total": round(total, 4),
        "currency": "USD",
        "daily": daily,
        "by_usage_type": by_usage_type,
        "per_cluster": per_cluster,
        "per_cluster_available": per_cluster_available,
        "per_cluster_tag": cluster_tag,
        "per_cluster_note": per_cluster_note,
        "anomalies": anomalies,
        "no_data_reason": no_data_reason,
        "discovered_services": services,
    })


# ===========================================================================
# RI / Savings Plan commitments path (?view=commitments)
# ---------------------------------------------------------------------------
# Compute Optimizer and our own scaling advice ignore Reserved Instances, so a
# "cheaper" instance-class recommendation can actually cost MORE when it breaks
# RI coverage (the RI keeps billing for a class you no longer run). This view
# surfaces the operator's real commitment posture so they can sanity-check any
# resize: active RDS RIs (per account+region derived from the clusters
# registry), a coarse cover/over/unused estimate (running instances vs RI
# count per class), and best-effort CE reservation/SP coverage for the hub
# account. Every external call fails soft to null/empty — we never leak str(e).
# ===========================================================================


def _session_for(region: str, role_arn: str = "") -> boto3.session.Session:
    """boto3 Session for a target account+region; assume `role_arn` when given
    (hub-spoke chaining), else a local session. Copied from api/clusters —
    api/ Lambdas are independent packages and cannot share imports."""
    if not role_arn:
        return boto3.session.Session(region_name=region or None)
    creds = boto3.client("sts").assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"dbops-cost-{datetime.utcnow().strftime('%H%M%S')}",
        DurationSeconds=900,
    )["Credentials"]
    return boto3.session.Session(
        region_name=region or None,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _account_from_arn(arn: str) -> str:
    """Account id from an ARN (arn:aws:rds:region:ACCOUNT:...). '' if unparsable."""
    parts = (arn or "").split(":")
    return parts[4] if len(parts) > 4 else ""


def _aurora_commitment_targets(event):
    """Scan the clusters registry, keep Aurora/RDS clusters VISIBLE to the
    caller, and return (targets, scan_failed) where targets is a list of
    distinct {account, region, role_arn} dicts. Account is parsed from
    cluster_arn (falls back to account_id); empty role = hub.

    Fail CLOSED: a registry-scan failure returns ([], True) so we surface
    nothing rather than risk leaking another tenant's accounts. (This is
    stricter than the rds view's fail-open per-cluster filter — commitments
    enumerate whole accounts, so a mis-scoped result is worse.)"""
    table_name = os.environ.get("CLUSTERS_TABLE", "")
    if not table_name:
        return [], False
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        resp = table.scan()
        items = resp.get("Items", [])
        while resp.get("LastEvaluatedKey"):
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
    except Exception as e:
        print(f"[cost] commitments registry scan failed: {e}")
        return [], True

    visible = tenancy.visible_cluster_ids(event, items)  # None => admin (all)
    targets = {}
    for it in items:
        if not (it.get("engine") or "").lower().startswith("aurora"):
            continue
        if visible is not None and it.get("cluster_id") not in visible:
            continue
        region = it.get("region") or ""
        role = it.get("spoke_role_arn") or ""
        account = _account_from_arn(it.get("cluster_arn")) or it.get("account_id") or ""
        targets.setdefault(
            (account, region, role),
            {"account": account, "region": region, "role_arn": role},
        )
    return list(targets.values()), False


def _describe_active_ris(rds, account: str, region: str) -> list:
    """Active RDS Reserved Instances in one account+region. RDS RIs carry no
    end field — end = StartTime + Duration. Fails soft to []."""
    rows = []
    try:
        marker = None
        while True:
            kwargs = {"MaxRecords": 100}
            if marker:
                kwargs["Marker"] = marker
            resp = rds.describe_reserved_db_instances(**kwargs)
            for ri in resp.get("ReservedDBInstances", []):
                if ri.get("State") != "active":
                    continue
                start = ri.get("StartTime")
                end_iso, remaining = None, None
                if start is not None:
                    end_dt = start + timedelta(seconds=ri.get("Duration") or 0)
                    end_iso = end_dt.isoformat()
                    remaining = (end_dt - datetime.now(timezone.utc)).days
                rows.append({
                    "account": account,
                    "region": region,
                    "instance_class": ri.get("DBInstanceClass", ""),
                    "count": int(ri.get("DBInstanceCount") or 0),
                    "multi_az": bool(ri.get("MultiAZ")),
                    "offering_type": ri.get("OfferingType", ""),
                    "product": ri.get("ProductDescription", ""),
                    "end": end_iso,
                    "remaining_days": remaining,
                })
            marker = resp.get("Marker")
            # Real boto Markers are non-empty strings; anything else terminates.
            if not isinstance(marker, str) or not marker:
                break
    except Exception as e:
        print(f"[cost] describe_reserved_db_instances failed ({account}/{region}): {e}")
    return rows


def _running_aurora_counts(rds) -> dict:
    """{instance_class: count} of running Aurora instances in one account+
    region — the denominator for the coarse cover/over/unused estimate. Fails
    soft to {}."""
    counts = {}
    try:
        marker = None
        while True:
            kwargs = {"MaxRecords": 100}
            if marker:
                kwargs["Marker"] = marker
            resp = rds.describe_db_instances(**kwargs)
            for inst in resp.get("DBInstances", []):
                if not (inst.get("Engine") or "").lower().startswith("aurora"):
                    continue
                cls = inst.get("DBInstanceClass") or ""
                counts[cls] = counts.get(cls, 0) + 1
            marker = resp.get("Marker")
            if not isinstance(marker, str) or not marker:
                break
    except Exception as e:
        print(f"[cost] describe_db_instances failed: {e}")
    return counts


def _reservation_coverage(ce, start, end):
    """Hub-account RDS reservation + Savings Plans coverage %, best-effort.
    Returns {reservation_pct, savings_plans_pct} (either may be None), or None
    if BOTH CE calls fail. Never leaks str(e)."""
    cov = {"reservation_pct": None, "savings_plans_pct": None}
    got = False
    try:
        resp = ce.get_reservation_coverage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Filter={"Dimensions": {"Key": "SERVICE",
                                   "Values": ["Amazon Relational Database Service"]}},
        )
        pct = (resp.get("Total") or {}).get("CoverageHours", {}).get("CoverageHoursPercentage")
        cov["reservation_pct"] = round(float(pct), 1) if pct is not None else None
        got = True
    except Exception as e:
        print(f"[cost] get_reservation_coverage failed: {e}")
    try:
        # SP doesn't cover RDS, but the operator may run committed compute
        # (EC2/Lambda/Fargate) — surfaced account-wide for full context.
        resp = ce.get_savings_plans_coverage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        )
        rows = resp.get("SavingsPlansCoverages") or []
        pct = rows[0].get("Coverage", {}).get("CoveragePercentage") if rows else None
        cov["savings_plans_pct"] = round(float(pct), 1) if pct is not None else None
        got = True
    except Exception as e:
        print(f"[cost] get_savings_plans_coverage failed: {e}")
    return cov if got else None


def _savings_plans_list():
    """Active Savings Plans (best-effort). None when the API/permission/bundled
    botocore is unavailable — SP is optional context, not a hard dependency."""
    try:
        sp = boto3.client("savingsplans", region_name="us-east-1")
        resp = sp.describe_savings_plans(states=["active"])
    except Exception as e:
        print(f"[cost] describe_savings_plans unavailable: {e}")
        return None
    return [
        {
            "type": p.get("savingsPlanType", ""),
            "commitment": p.get("commitment", ""),
            "end": p.get("end", ""),
        }
        for p in resp.get("savingsPlans", [])
    ]


def _handle_commitments_view(ce, start, end, days, event=None):
    """RI/SP posture for the caller's visible Aurora accounts. Envelope:
    {ris, summary:{total, expiring_30d, unused_estimate}, coverage|null,
     savings_plans|null, note}."""
    targets, scan_failed = _aurora_commitment_targets(event)

    ris = []
    running_by_ar = {}  # (account, region) -> {class: running_count}
    for t in targets:
        try:
            rds = _session_for(t["region"], t["role_arn"]).client("rds")
        except Exception as e:
            print(f"[cost] assume/session failed ({t['account']}/{t['region']}): {e}")
            continue
        ris.extend(_describe_active_ris(rds, t["account"], t["region"]))
        running_by_ar[(t["account"], t["region"])] = _running_aurora_counts(rds)

    total = sum(r["count"] for r in ris)
    expiring_30d = sum(
        r["count"] for r in ris
        if r.get("remaining_days") is not None and r["remaining_days"] <= 30
    )
    # Coarse unused estimate: per (account, region, class), RI count above the
    # number of running instances of that class = RIs paying for nothing.
    ri_by_class = {}
    for r in ris:
        key = (r["account"], r["region"], r["instance_class"])
        ri_by_class[key] = ri_by_class.get(key, 0) + r["count"]
    unused_estimate = 0
    for (account, region, cls), ri_ct in ri_by_class.items():
        running = running_by_ar.get((account, region), {}).get(cls, 0)
        unused_estimate += max(0, ri_ct - running)

    # RIs with the soonest expiry first so the UI D-day badges lead.
    ris.sort(key=lambda r: (r["remaining_days"] if r.get("remaining_days") is not None else 10**9))

    if scan_failed:
        note = "클러스터 레지스트리 조회 실패로 커밋 할인 현황을 표시할 수 없습니다."
    elif not targets:
        note = "등록된 Aurora/RDS 클러스터가 없어 조회할 계정이 없습니다."
    else:
        note = (
            "커버/초과/미사용 추정치는 계정·리전·클래스별 실행 중인 인스턴스 수와 "
            "보유 RI 수량을 비교한 근사치입니다. RI 실효 절감은 계약 조건(선결제·기간)에 "
            "따라 달라집니다. CE 커버리지는 허브 계정 기준입니다."
        )

    return _response(200, {
        "env": _ENV,
        "view": "commitments",
        "range_days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "ris": ris,
        "summary": {
            "total": total,
            "expiring_30d": expiring_30d,
            "unused_estimate": unused_estimate,
        },
        "coverage": _reservation_coverage(ce, start, end),
        "savings_plans": _savings_plans_list(),
        "note": note,
    })


# ===========================================================================
# DBOps platform cost path (?view=platform)
# ---------------------------------------------------------------------------
# "DBOps 자체를 돌리는 데 얼마 드나" — every CDK-managed resource carries
# Application=DBOps (app.py adds the tag app-wide), and that tag is already
# activated for cost allocation (the Bedrock view depends on it). So one
# tag-filtered CE query covers the whole platform: Lambdas, the Aurora cache,
# DynamoDB, CloudFront/S3, AgentCore, logs… Monitored CUSTOMER clusters are
# NOT tagged and therefore excluded by construction; the only RDS spend in
# here is the cache DB (+ any CDK-deployed sample clusters).
# ===========================================================================

def _handle_platform_view(ce, start, end, days):
    tag_filter = {"Tags": {"Key": "Application", "Values": ["DBOps"]}}

    daily, total, total_err = [], 0.0, None
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            Filter=tag_filter,
        )
        for r in resp.get("ResultsByTime", []):
            amount = float(r["Total"]["UnblendedCost"]["Amount"])
            daily.append({"date": r["TimePeriod"]["Start"], "amount": amount})
            total += amount
    except Exception as e:
        print(f"[cost] platform total failed: {e}")
        msg = str(e).lower()
        if "not activated" in msg or "is not currently activated" in msg:
            total_err = "cost_allocation_tag_not_activated"
        else:
            total_err = "cost_explorer_query_failed"

    # Service-level breakdown over the same window.
    by_service = []
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter=tag_filter,
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        rollup = {}
        for r in resp.get("ResultsByTime", []):
            for g in r.get("Groups", []):
                svc = g["Keys"][0]
                amt = float(g["Metrics"]["UnblendedCost"]["Amount"])
                rollup[svc] = rollup.get(svc, 0.0) + amt
        by_service = [
            {"service": k, "amount": round(v, 4)}
            for k, v in sorted(rollup.items(), key=lambda kv: kv[1], reverse=True)
            if round(v, 4) != 0.0
        ]
    except Exception as e:
        print(f"[cost] platform by_service failed: {e}")

    no_data_reason = None
    if total_err == "cost_allocation_tag_not_activated":
        no_data_reason = (
            "Application 태그가 cost allocation tag로 활성화되지 않았습니다 — "
            "AWS Billing 콘솔에서 활성화하면 ~24시간 후부터 집계됩니다."
        )
    elif total == 0 and not daily:
        no_data_reason = (
            "이 기간에 Application=DBOps 태그가 붙은 비용이 없습니다. "
            "배포 직후라면 Cost Explorer 반영(~24h)을 기다려 주세요."
        )

    return _response(200, {
        "env": _ENV,
        "view": "platform",
        "range_days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total": round(total, 4),
        "currency": "USD",
        "daily": daily,
        "by_service": by_service,
        "anomalies": _detect_anomalies(daily),
        "no_data_reason": no_data_reason,
        "note": (
            "Application=DBOps 태그 기준 — 모니터링 대상 고객 클러스터는 태그가 "
            "없어 제외됩니다. RDS 항목은 DBOps 캐시 DB(+CDK 샘플 클러스터)이며, "
            "Bedrock 항목은 Bedrock 탭과 동일한 비용입니다."
        ),
    })


def _handle_tokens_view(start, end, days):
    """Fleet Bedrock token usage by model + daily series, from CloudWatch
    AWS/Bedrock metrics. NOTE: these metrics are not tag-filterable, so this is
    account-wide Bedrock token usage (same untagged scope the cost views note)."""
    cw = boto3.client("cloudwatch")
    # Discover model ids from the InputTokenCount metric's ModelId dimension.
    try:
        metrics = cw.list_metrics(Namespace="AWS/Bedrock", MetricName="InputTokenCount").get("Metrics", [])
    except Exception as e:
        print(f"[cost] list_metrics (AWS/Bedrock InputTokenCount) failed: {e}")
        return _response(200, {"view": "tokens", "days": days, "by_model": [], "daily": [],
                               "note": "CloudWatch 토큰 메트릭 목록 조회에 실패했습니다 "
                                       "(자세한 원인은 서버 로그를 확인하세요)."})
    model_ids = sorted({
        d["Value"] for m in metrics for d in m.get("Dimensions", []) if d["Name"] == "ModelId"
    })
    if not model_ids:
        return _response(200, {"view": "tokens", "days": days, "by_model": [], "daily": [],
                               "note": "Bedrock 토큰 메트릭 없음 — 아직 모델 호출 기록이 없거나 메트릭 전파 전입니다."})

    # Build GetMetricData queries: per model, Input + Output, Sum, daily period.
    queries, idmap = [], {}
    for i, mid in enumerate(model_ids):
        for kind, metric in (("input", "InputTokenCount"), ("output", "OutputTokenCount")):
            qid = f"m{i}_{kind}"
            idmap[qid] = (mid, kind)
            queries.append({
                "Id": qid,
                "MetricStat": {
                    "Metric": {"Namespace": "AWS/Bedrock", "MetricName": metric,
                               "Dimensions": [{"Name": "ModelId", "Value": mid}]},
                    "Period": 86400, "Stat": "Sum",
                },
                "ReturnData": True,
            })
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.min.time())
    # GetMetricData caps at 500 queries/call; our model count is tiny, so one call.
    try:
        resp = cw.get_metric_data(MetricDataQueries=queries[:500], StartTime=start_dt, EndTime=end_dt,
                                  ScanBy="TimestampAscending")
        totals = {mid: {"input": 0.0, "output": 0.0} for mid in model_ids}
        daily: dict = {}
        for res in resp.get("MetricDataResults", []):
            mid, kind = idmap.get(res["Id"], (None, None))
            if mid is None:
                continue
            for ts, val in zip(res.get("Timestamps", []), res.get("Values", []), strict=False):
                totals[mid][kind] += val
                day = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
                daily.setdefault(day, {"input": 0.0, "output": 0.0})[kind] += val
    except Exception as e:
        print(f"[cost] get_metric_data (Bedrock token counts) failed: {e}")
        return _response(200, {"view": "tokens", "days": days, "by_model": [], "daily": [],
                               "note": "CloudWatch 토큰 사용량 조회에 실패했습니다 "
                                       "(자세한 원인은 서버 로그를 확인하세요)."})
    by_model = [{"model": mid, "input": int(t["input"]), "output": int(t["output"]),
                 "total": int(t["input"] + t["output"])}
                for mid, t in totals.items()]
    by_model.sort(key=lambda m: m["total"], reverse=True)
    daily_list = [{"date": d, "input": int(v["input"]), "output": int(v["output"])}
                  for d, v in sorted(daily.items())]
    return _response(200, {"view": "tokens", "days": days, "by_model": by_model, "daily": daily_list,
                           "note": "계정 전체 Bedrock 토큰 사용량(모델별) — CloudWatch 메트릭은 태그 필터 불가."})


def lambda_handler(event, context=None):
    method = event.get("requestContext", {}).get("http", {}).get("method") \
        or event.get("httpMethod", "GET")
    if method != "GET":
        return _response(405, {"error": f"method {method} not allowed"})

    qs = event.get("queryStringParameters") or {}
    days = max(7, min(int(qs.get("days") or "30"), 90))
    end = datetime.utcnow().date()
    start = end - timedelta(days=days)

    view = (qs.get("view") or "bedrock").lower()

    # Tokens path uses CloudWatch, not Cost Explorer — dispatch before building ce.
    if view == "tokens":
        return _handle_tokens_view(start, end, days)

    ce = boto3.client("ce", region_name="us-east-1")  # CE is global; us-east-1 is the standard endpoint.

    # `?view=rds` switches from Bedrock spend to Aurora/RDS spend. Same Lambda,
    # same range windows; the RDS path has its own service discovery + per-
    # cluster (tag-based) attribution. Default view stays Bedrock.
    if view == "rds":
        return _handle_rds_view(ce, start, end, days, event)
    # `?view=elasticache` — ElastiCache 클러스터 비용.
    if view == "elasticache":
        return _handle_elasticache_view(ce, start, end, days, event)
    # `?view=commitments` — RI/Savings Plan 현황 (RI-aware 비용 분석).
    if view == "commitments":
        return _handle_commitments_view(ce, start, end, days, event)
    # `?view=platform` — DBOps 플랫폼 자체 운영비 (Application=DBOps 태그 전체).
    if view == "platform":
        return _handle_platform_view(ce, start, end, days)

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
