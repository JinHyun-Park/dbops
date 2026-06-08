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

from mcp_servers.shared.aurora_pricing import (
    price_per_acu_hour,
    price_per_instance_hour,
)
from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import lookup_cluster, rds_client_for_cluster

# Hours billed per month for a continuously-running instance. 730 = 365*24/12,
# the AWS convention for monthly estimates (Aurora bills ACU-hours / instance-hours).
HOURS_PER_MONTH = 730


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

    def _monthly(min_acu, max_acu):
        if price is None or min_acu is None or max_acu is None:
            return None
        midpoint = (min_acu + max_acu) / 2
        return round(midpoint * price * HOURS_PER_MONTH * member_count, 2)

    current_cost = _monthly(current_min, current_max)
    proposed_cost = _monthly(proposed_min, proposed_max)
    delta = round(proposed_cost - current_cost, 2) if (current_cost is not None and proposed_cost is not None) else None

    price_available = price is not None
    note = (
        f"중간값 ACU 기준 추정({HOURS_PER_MONTH}h, {member_count}개 인스턴스). "
        "리더는 라이터와 동일한 ACU 범위로 근사했습니다(API가 인스턴스별 설정을 노출하지 않음). "
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
        # emit fabricated numbers or raise.
        return _degraded_result(
            cluster_id,
            new_min_acu,
            new_max_acu,
            new_instance_class,
            region,
            "라이브 클러스터 조회 실패로 현재 구성을 확인할 수 없습니다 "
            f"({type(e).__name__}). 비용 비교를 생략합니다.",
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
        return _serverless_result(
            cluster_id, cluster, region, engine, io_optimized,
            writers, readers, member_count, new_min_acu, new_max_acu,
        )

    return _provisioned_result(
        cluster_id, rds, members, region, engine, io_optimized,
        writers, readers, member_count, new_instance_class,
    )
