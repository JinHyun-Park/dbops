"""scaling_simulation — estimate the monthly cost impact of resizing an Aurora
cluster, for BOTH Serverless v2 (ACU range) AND provisioned (instance class).

WHY this rewrite: the previous version (a) only understood Serverless v2 and
(b) baked in a single us-east-1 Standard ACU rate ($0.12), which is wrong almost
everywhere — Seoul I/O-Optimized is $0.26/ACU-hr. Directional "save vs spend"
guidance built on a wrong unit price misleads DBAs in every non-us-east-1 region.

This version grounds every number in LIVE facts:
- The cluster's real region, engine, storage edition (Standard vs I/O-Optimized),
  member topology and (for provisioned) instance classes come from a
  cross-account-aware RDS describe (`rds_client_for_cluster` + `lookup_cluster`).
- The unit price comes from the AWS Price List API via the already-smoke-tested
  `aurora_pricing` helpers, resolved for THAT region/engine/edition.

Every external lookup fails soft: if the describe is unavailable we return a
graceful estimate dict (costs None) and if a price is unavailable we null that
cost and mark the source as a fallback — we NEVER fabricate a baseline and we
NEVER raise.
"""

import os
from datetime import datetime, timedelta, timezone

from mcp_servers.shared.aurora_pricing import (
    price_per_acu_hour,
    price_per_instance_hour,
)
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import (
    client_for_cluster,
    lookup_cluster,
    rds_client_for_cluster,
)

# Hours billed per month for a continuously-running instance. 730 = 365*24/12,
# the AWS convention for monthly estimates (Aurora bills ACU-hours / instance-hours).
HOURS_PER_MONTH = 730

# Lookback for the OBSERVED average ACU (CloudWatch). Serverless v2 bills the
# actual ACU it ran at, so the real long-run cost tracks the observed average,
# not the min/max midpoint.
_ACU_LOOKBACK_DAYS = 14


def _observed_avg_acu(cluster_id: str):
    """(avg_acu, basis) from CloudWatch ServerlessDatabaseCapacity over the
    lookback, in the cluster's own account; (None, reason) when unavailable.

    This is what makes the serverless cost REAL instead of a midpoint guess: a
    cluster that idles near its min most of the day costs far less than
    (min+max)/2 implies. Fails soft so a missing metric/permission degrades to
    the midpoint fallback rather than raising."""
    try:
        cw = client_for_cluster(cluster_id, "cloudwatch")
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=_ACU_LOOKBACK_DAYS)
        resp = cw.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="ServerlessDatabaseCapacity",
            Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
            StartTime=start,
            EndTime=end,
            Period=3600,
            Statistics=["Average"],
        )
        pts = [d["Average"] for d in resp.get("Datapoints", []) if d.get("Average") is not None]
        if not pts:
            return None, "CloudWatch ServerlessDatabaseCapacity 데이터포인트 없음"
        return sum(pts) / len(pts), f"CloudWatch {_ACU_LOOKBACK_DAYS}일 평균 ACU ({len(pts)} 포인트)"
    except Exception as e:  # pragma: no cover - defensive soft-fail
        print(f"[scaling_simulation] observed ACU lookup failed for {cluster_id}: {e}")
        return None, "관측 ACU 조회 실패 (자세한 원인은 서버 로그를 확인하세요)"


def _change_pct(current, proposed):
    """Percent change from current to proposed, safe when `current` is 0 or
    None (avoids ZeroDivisionError and meaningless ±inf%). Returns None when a
    percentage can't be computed so callers render "n/a" instead of a fake 0."""
    if not current:
        return None
    if proposed is None:
        return None
    return round((proposed - current) / current * 100, 1)


def _resolve_region(cluster_id: str) -> str:
    """Region for pricing/topology: prefer the clusters-registry row (so a
    cross-account spoke cluster prices in ITS region, not the runtime's), then
    fall back to the runtime's AWS_REGION. WHY registry-first: the same price
    lookup must use the same region the describe targeted."""
    row = lookup_cluster(cluster_id)
    return row.get("region") or os.environ.get("AWS_REGION", "")


def _member_instance_classes(rds, cluster_id: str) -> dict:
    """Map DBInstanceIdentifier -> DBInstanceClass for a provisioned cluster.

    WHY a separate describe: DBClusterMembers in describe_db_clusters does NOT
    carry the instance class, so we resolve it from describe_db_instances
    filtered to this cluster. Fails soft (returns {}) so a missing permission or
    transient error degrades to "unknown class" rather than crashing the sim."""
    try:
        resp = rds.describe_db_instances(
            Filters=[{"Name": "db-cluster-id", "Values": [cluster_id]}]
        )
        return {
            inst.get("DBInstanceIdentifier"): inst.get("DBInstanceClass")
            for inst in resp.get("DBInstances", [])
            if inst.get("DBInstanceIdentifier")
        }
    except Exception as e:  # pragma: no cover - defensive soft-fail
        print(f"[scaling_simulation] describe_db_instances failed for {cluster_id}: {e}")
        return {}


# Provisioned instance-size ladder for the "upsize one class" comparison. Same
# family (e.g. db.r6g.*), one step up the size axis. Missing/unknown size => no
# next class (autoscale_vs_fixed is then omitted), never a fabricated guess.
# "micro"/"small" are prepended for T-family (db.t3/db.t4g) RDS instances
# (rds_instance family, R-5 right-sizing) — no Aurora r6g/etc class ever uses
# those size tokens, so this is additive and doesn't change Aurora behavior.
_SIZE_LADDER = [
    "micro", "small", "medium", "large", "xlarge", "2xlarge", "4xlarge", "8xlarge",
    "12xlarge", "16xlarge", "24xlarge", "32xlarge", "48xlarge",
]


def _next_class_up(instance_class: str):
    """The next larger instance class in the same family, or None if it can't
    be resolved (unknown size token / top of the ladder / not a db.<fam>.<size>)."""
    if not instance_class:
        return None
    prefix, _, size = instance_class.rpartition(".")
    if not prefix:
        return None
    try:
        i = _SIZE_LADDER.index(size)
    except ValueError:
        return None
    if i + 1 >= len(_SIZE_LADDER):
        return None
    return f"{prefix}.{_SIZE_LADDER[i + 1]}"


def _active_ris(rds) -> list:
    """Active RDS Reserved Instances in the cluster's account+region (via the
    same cross-account rds client the describe used). RDS RIs carry no end
    field — end = StartTime + Duration. Fails soft to []; never raises."""
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
                end_date = None
                if start is not None:
                    end_date = (start + timedelta(seconds=ri.get("Duration") or 0)).date().isoformat()
                rows.append({
                    "instance_class": ri.get("DBInstanceClass", ""),
                    "count": int(ri.get("DBInstanceCount") or 0),
                    "end": end_date,
                })
            marker = resp.get("Marker")
            # Real boto Markers are non-empty strings; anything else (None, or a
            # bare Mock in tests) terminates paging so we never loop forever.
            if not isinstance(marker, str) or not marker:
                break
    except Exception as e:  # pragma: no cover - defensive soft-fail
        print(f"[scaling_simulation] describe_reserved_db_instances failed: {e}")
    return rows


def _ri_match(ris: list, instance_class: str) -> dict:
    """RI coverage summary for one instance class: match flag, covered count,
    and the latest expiry among matching RIs."""
    matched = [r for r in ris if instance_class and r["instance_class"] == instance_class]
    return {
        "instance_class": instance_class,
        "ri_match": bool(matched),
        "ri_count": sum(r["count"] for r in matched),
        "expires": max((r["end"] for r in matched if r.get("end")), default=None),
    }


def _commitment_context(rds, region, engine, io_optimized, result) -> dict:
    """Best-effort RI-awareness annotation for a scaling result. Reports
    whether the current/proposed instance classes are RI-covered, warns when a
    resize would leave an RI stranded, and (provisioned) compares scale-out vs
    scale-up on ON-DEMAND unit prices only — never a fabricated RI discount.
    Output-only; any failure yields {"available": False}."""
    ris = _active_ris(rds)
    mode = result.get("mode")
    current_class = (result.get("current") or {}).get("instance_class")
    proposed_class = (result.get("proposed") or {}).get("instance_class")

    if mode == "serverless":
        note = None
        if ris:
            note = (
                f"Aurora Serverless v2 용량(ACU)은 인스턴스 RI로 커버되지 않습니다. "
                f"이 계정/리전의 보유 RI {sum(r['count'] for r in ris)}건은 프로비저닝 "
                "인스턴스에만 적용됩니다."
            )
        return {
            "available": True,
            "region": region,
            "current_class": None,
            "proposed_class": None,
            "note": note,
            "autoscale_vs_fixed": None,
        }

    cur = _ri_match(ris, current_class)
    prop = _ri_match(ris, proposed_class)

    note = None
    # A resize AWAY from a covered class onto an uncovered one strands the RI.
    if (
        proposed_class and current_class and proposed_class != current_class
        and cur["ri_match"] and not prop["ri_match"]
    ):
        until = f" 만료 {cur['expires']}까지" if cur.get("expires") else ""
        note = (
            f"제안 클래스({proposed_class})는 보유 RI에 없음 — 변경분은 온디맨드로 "
            f"과금되어 표시된 절감액이 실효 절감과 다를 수 있습니다.{until} 기존 RI는 "
            "미사용으로 남습니다."
        )

    # Scale-out vs scale-up, ON-DEMAND unit prices only. RI-covered sides are
    # flagged (not re-priced) so we never invent a contract rate.
    autoscale_vs_fixed = None
    reader_price = price_per_instance_hour(region, engine, current_class, io_optimized) if current_class else None
    next_class = _next_class_up(current_class)
    next_price = price_per_instance_hour(region, engine, next_class, io_optimized) if next_class else None
    if reader_price is not None and next_price is not None:
        autoscale_vs_fixed = {
            "add_reader": {
                "instance_class": current_class,
                "monthly_on_demand_usd": round(reader_price * HOURS_PER_MONTH, 2),
                "ri_covered": cur["ri_match"],
            },
            "upsize_writer": {
                "instance_class": next_class,
                "delta_monthly_on_demand_usd": round((next_price - reader_price) * HOURS_PER_MONTH, 2),
                "ri_covered": _ri_match(ris, next_class)["ri_match"],
            },
            "note": (
                "추정치이며 온디맨드 단가 기준입니다(리더 1대 추가 vs 라이터 한 단계 "
                "상향). RI 보유분은 실효가가 계약 조건에 따라 달라집니다."
            ),
        }

    return {
        "available": True,
        "region": region,
        "current_class": cur,
        "proposed_class": prop,
        "note": note,
        "autoscale_vs_fixed": autoscale_vs_fixed,
    }


def _degraded_result(cluster_id: str, new_min_acu, new_max_acu, new_instance_class, region, note: str) -> dict:
    """Build the graceful "live describe unavailable" payload. WHY a helper:
    keeps the no-data path honest — every cost is None, mode is best-effort, and
    the shape still matches the contract so the REST mirror / frontend don't break."""
    mode = "provisioned" if new_instance_class else "serverless"
    if mode == "serverless":
        current = {"min_acu": None, "max_acu": None}
        proposed = {"min_acu": new_min_acu, "max_acu": new_max_acu}
        kind = "acu"
    else:
        current = {"instance_class": None}
        proposed = {"instance_class": new_instance_class}
        kind = "instance"
    return {
        "cluster_id": cluster_id,
        "mode": mode,
        "current": current,
        "proposed": proposed,
        "writers": 0,
        "readers": 0,
        "cost_impact": {
            "current_monthly_usd": None,
            "proposed_monthly_usd": None,
            "delta_monthly_usd": None,
            "change_pct": None,
        },
        "unit_pricing": {
            "kind": kind,
            "price_per_hour": None,
            "region": region,
            "io_optimized": False,
            "source": "fallback",
        },
        "data_source": "estimate (live describe unavailable)",
        "note": note,
        "commitment_context": {"available": False},
    }


def _serverless_result(
    cluster_id, cluster, region, engine, io_optimized, writers, readers, member_count, new_min_acu, new_max_acu
) -> dict:
    """Serverless v2 cost model: midpoint(min,max) * $/ACU-hr * 730 * members.

    WHY the midpoint: Serverless v2 scales continuously between min and max on
    load; absent per-second telemetry the long-run average is best approximated
    by the midpoint. WHY *member_count: each reader is its own Serverless v2
    instance billing its own ACU-hours; we approximate every member at the same
    ACU range because describe_db_clusters doesn't expose per-instance config."""
    scaling = cluster.get("ServerlessV2ScalingConfiguration") or {}
    current_min = float(scaling.get("MinCapacity")) if scaling.get("MinCapacity") is not None else None
    current_max = float(scaling.get("MaxCapacity")) if scaling.get("MaxCapacity") is not None else None
    proposed_min = float(new_min_acu) if new_min_acu is not None else current_min
    proposed_max = float(new_max_acu) if new_max_acu is not None else current_max

    price = price_per_acu_hour(region, engine, io_optimized)

    # OBSERVED average ACU (CloudWatch) is the real billing driver; the midpoint
    # is only a fallback when we have no telemetry. The observed draw is clamped
    # into each range to model "the same workload under these new bounds".
    observed_acu, acu_basis_note = _observed_avg_acu(cluster_id)

    def _effective_acu(min_acu, max_acu):
        if observed_acu is not None:
            if min_acu is None or max_acu is None:
                return observed_acu
            return max(min_acu, min(observed_acu, max_acu))  # clamp into [min,max]
        if min_acu is None or max_acu is None:
            return None
        return (min_acu + max_acu) / 2  # midpoint fallback

    def _monthly(min_acu, max_acu):
        acu = _effective_acu(min_acu, max_acu)
        if price is None or acu is None:
            return None
        return round(acu * price * HOURS_PER_MONTH * member_count, 2)

    current_cost = _monthly(current_min, current_max)
    proposed_cost = _monthly(proposed_min, proposed_max)
    delta = round(proposed_cost - current_cost, 2) if (current_cost is not None and proposed_cost is not None) else None

    price_available = price is not None
    acu_basis = "observed" if observed_acu is not None else "midpoint"
    confidence = "high" if (price_available and observed_acu is not None) else "low"
    basis_phrase = (
        f"관측 평균 {round(observed_acu, 2)} ACU 기준({acu_basis_note})"
        if observed_acu is not None
        else f"중간값 ACU 기준 추정({acu_basis_note} — 관측 ACU 없음)"
    )
    note = (
        f"{basis_phrase}, {HOURS_PER_MONTH}h × {member_count}개 인스턴스. "
        "리더는 라이터와 동일한 ACU로 근사했습니다(API가 인스턴스별 설정을 노출하지 않음). "
        + (
            f"단가는 AWS Price List API 기준 ${price}/ACU-hr "
            f"(region={region}, IO-Optimized={io_optimized})입니다. "
            "ACU 변경은 즉시 적용되며 다운타임이 없습니다."
            if price_available
            else f"단가 조회 실패(region={region})로 비용을 추정할 수 없습니다."
        )
    )

    return {
        "cluster_id": cluster_id,
        "mode": "serverless",
        "current": {"min_acu": current_min, "max_acu": current_max},
        "proposed": {"min_acu": proposed_min, "max_acu": proposed_max},
        "writers": writers,
        "readers": readers,
        "observed_avg_acu": round(observed_acu, 2) if observed_acu is not None else None,
        "acu_basis": acu_basis,
        "confidence": confidence,
        "cost_impact": {
            "current_monthly_usd": current_cost,
            "proposed_monthly_usd": proposed_cost,
            "delta_monthly_usd": delta,
            "change_pct": _change_pct(current_cost, proposed_cost),
        },
        "unit_pricing": {
            "kind": "acu",
            "price_per_hour": price,
            "region": region,
            "io_optimized": io_optimized,
            "source": "aws_pricing_api" if price_available else "fallback",
        },
        "data_source": "live (describe_db_clusters)" if price_available else "estimate (pricing unavailable)",
        "note": note,
    }


def _provisioned_result(
    cluster_id, rds, members, region, engine, io_optimized, writers, readers, member_count, new_instance_class
) -> dict:
    """Provisioned cost model: current = sum over members of
    $/instance-hr(member_class) * 730; proposed = member_count * $/instance-hr(
    new_class) * 730 when a new class is given.

    WHY current = the writer's (or most common) class for `current.instance_class`:
    the contract surfaces a single representative class; we report the writer's
    class because that's the cluster's headline size. WHY proposed approximates
    every member at the new class: a resize typically applies uniformly, and the
    note flags that readers are approximated at the writer/new class."""
    id_to_class = _member_instance_classes(rds, cluster_id)

    # Resolve each member's class (fall back to "unknown" if the instance
    # describe couldn't map it), and pick the headline class for `current`.
    member_classes = []
    writer_class = None
    for m in members:
        ident = m.get("DBInstanceIdentifier")
        klass = id_to_class.get(ident) or m.get("DBInstanceClass")
        member_classes.append(klass)
        if m.get("IsClusterWriter") and klass:
            writer_class = klass
    if writer_class is None:
        # No writer class resolved: use the most common known class.
        known = [c for c in member_classes if c]
        writer_class = max(set(known), key=known.count) if known else None

    # Current monthly = sum of each member's instance-hour price * 730.
    current_cost = 0.0
    current_priced = True
    for klass in member_classes:
        price = price_per_instance_hour(region, engine, klass, io_optimized) if klass else None
        if price is None:
            current_priced = False
            break
        current_cost += price * HOURS_PER_MONTH
    current_cost = round(current_cost, 2) if current_priced and member_classes else None

    # Proposed: resize every member to new_instance_class, else mirror current.
    proposed_class = new_instance_class or writer_class
    if new_instance_class:
        new_price = price_per_instance_hour(region, engine, new_instance_class, io_optimized)
        proposed_cost = round(member_count * new_price * HOURS_PER_MONTH, 2) if new_price is not None else None
    else:
        proposed_cost = current_cost

    delta = round(proposed_cost - current_cost, 2) if (current_cost is not None and proposed_cost is not None) else None

    # `price_per_hour` reports the proposed class's unit price (the knob being
    # simulated); fall back to the writer's class price when no new class given.
    headline_price = price_per_instance_hour(region, engine, proposed_class, io_optimized) if proposed_class else None
    price_available = current_cost is not None or proposed_cost is not None

    note = (
        f"프로비저닝 인스턴스 기준 추정({HOURS_PER_MONTH}h, {member_count}개 인스턴스). "
        "리더는 라이터와 동일한 인스턴스 클래스로 근사했습니다. "
        + (
            f"단가는 AWS Price List API 기준 (region={region}, IO-Optimized={io_optimized})입니다."
            if price_available
            else f"인스턴스 단가 조회 실패(region={region})로 비용을 추정할 수 없습니다."
        )
    )

    return {
        "cluster_id": cluster_id,
        "mode": "provisioned",
        "current": {"instance_class": writer_class},
        "proposed": {"instance_class": proposed_class},
        "writers": writers,
        "readers": readers,
        "cost_impact": {
            "current_monthly_usd": current_cost,
            "proposed_monthly_usd": proposed_cost,
            "delta_monthly_usd": delta,
            "change_pct": _change_pct(current_cost, proposed_cost),
        },
        "unit_pricing": {
            "kind": "instance",
            "price_per_hour": headline_price,
            "region": region,
            "io_optimized": io_optimized,
            "source": "aws_pricing_api" if (headline_price is not None) else "fallback",
        },
        "data_source": "live (describe_db_clusters)" if price_available else "estimate (pricing unavailable)",
        "note": note,
    }


def simulate_scaling_impl(
    cache: CacheClient,
    cluster_id: str,
    new_min_acu: float = None,
    new_max_acu: float = None,
    new_instance_class: str = None,
) -> dict:
    """Compare a cluster's CURRENT (live) size against a PROPOSED size and
    estimate the monthly cost delta using REAL AWS prices.

    Supports both deployment modes:
    - Serverless v2 (cluster has ServerlessV2ScalingConfiguration): vary the ACU
      range via `new_min_acu` / `new_max_acu`; cost uses $/ACU-hr.
    - Provisioned (no ACU config): resize the instance class via
      `new_instance_class`; cost uses $/instance-hr per member.

    All facts come from a cross-account-aware RDS describe and all prices from
    the AWS Price List API. If the describe fails entirely we return a graceful
    estimate dict (costs None). If a price is unavailable we null that cost and
    mark the source as a fallback. This function never raises.

    `cache` is accepted for signature compatibility with the MCP dispatcher; the
    live RDS/pricing path is authoritative so the cache isn't queried here.
    """
    region = _resolve_region(cluster_id)

    try:
        rds = rds_client_for_cluster(cluster_id)
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        clusters = resp.get("DBClusters", [])
        cluster = clusters[0] if clusters else None
    except Exception as e:
        # Cross-account / unregistered / unreachable: degrade gracefully. Do NOT
        # emit fabricated numbers or raise. The reason is STATIC (not even the
        # exception class): this string reaches the agent transcript, and the
        # DBA cannot act on a Python class name. Full detail to CloudWatch.
        print(f"[scaling_simulation] describe_db_clusters failed for {cluster_id}: {e}")
        return _degraded_result(
            cluster_id,
            new_min_acu,
            new_max_acu,
            new_instance_class,
            region,
            "라이브 클러스터 조회 실패로 현재 구성을 확인할 수 없습니다 "
            "(자세한 원인은 서버 로그를 확인하세요). 비용 비교를 생략합니다.",
        )

    if cluster is None:
        return _degraded_result(
            cluster_id,
            new_min_acu,
            new_max_acu,
            new_instance_class,
            region,
            "describe_db_clusters가 해당 cluster_id를 반환하지 않았습니다. 비용 비교를 생략합니다.",
        )

    engine = cluster.get("Engine") or ""
    # I/O-Optimized clusters carry StorageType "aurora-iopt1"; this flips the
    # whole price table (ACU and instance), so it MUST drive the pricing lookup.
    io_optimized = cluster.get("StorageType") == "aurora-iopt1"

    members = cluster.get("DBClusterMembers", [])
    writers = sum(1 for m in members if m.get("IsClusterWriter"))
    readers = sum(1 for m in members if not m.get("IsClusterWriter"))
    # Always bill at least one instance even if the API omitted member roles.
    member_count = max(1, writers + readers)

    if cluster.get("ServerlessV2ScalingConfiguration"):
        result = _serverless_result(
            cluster_id, cluster, region, engine, io_optimized,
            writers, readers, member_count, new_min_acu, new_max_acu,
        )
    else:
        result = _provisioned_result(
            cluster_id, rds, members, region, engine, io_optimized,
            writers, readers, member_count, new_instance_class,
        )

    # RI-aware annotation (output-only, best-effort). A failure here must never
    # touch the existing result — the cost sim is authoritative on its own.
    try:
        result["commitment_context"] = _commitment_context(rds, region, engine, io_optimized, result)
    except Exception as e:  # pragma: no cover - defensive soft-fail
        print(f"[scaling_simulation] commitment_context failed for {cluster_id}: {e}")
        result["commitment_context"] = {"available": False}
    return result
