"""scaling_simulation — estimate the cost/footprint impact of changing an
Aurora Serverless v2 cluster's ACU range.

WHY this is data-driven, not hardcoded: the previous version read
`cluster_meta` and then ignored it, fabricating the SAME current state
(min 0.5 / max 4.0 ACU, 2-ACU cost) for every cluster. That made the
"current vs proposed" comparison meaningless — it compared the proposal
against a constant, not the cluster's real configuration.

This version grounds "current" in the cluster's LIVE RDS configuration via a
cross-account-aware client (`rds_client_for_cluster` resolves the cluster's
region + spoke_role_arn from the registry and assumes the role). The
simulation Lambda now has rds:DescribeDBClusters + sts:AssumeRole. When the
live describe is unavailable (unregistered / cross-account failure /
non-serverless engine) we DEGRADE GRACEFULLY and say so, rather than emitting
fabricated numbers.
"""

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.cluster_targets import rds_client_for_cluster

# Hours billed per month for a continuously-running instance (Aurora bills
# ACU-hours). 730 = 365 * 24 / 12, the AWS convention for monthly estimates.
HOURS_PER_MONTH = 730

# Aurora Serverless v2 list price per ACU-hour. WHY a module constant with a
# caveat: the real rate is region- and edition-dependent (Standard vs
# I/O-Optimized) and changes over time, so this is an APPROXIMATION for
# directional cost guidance, not a billing figure. ~$0.12/ACU-hr is the
# us-east-1 I/O-Optimized ballpark.
ACU_PRICE_PER_HOUR = 0.12


def _midpoint_monthly_cost(min_acu: float, max_acu: float, member_count: int) -> float:
    """Estimate monthly cost for `member_count` instances each oscillating in
    the [min_acu, max_acu] range.

    WHY the midpoint: Serverless v2 scales continuously between min and max
    based on load; without per-second telemetry the long-run average is best
    approximated by the midpoint of the range. WHY multiply by member_count:
    each reader is its own Serverless v2 instance that bills its own ACU-hours.
    We approximate every reader at the SAME ACU range as the writer because the
    DescribeDBClusters response does not expose per-instance scaling config —
    this is documented as an approximation in the returned `note`.
    """
    midpoint = (min_acu + max_acu) / 2
    return midpoint * ACU_PRICE_PER_HOUR * HOURS_PER_MONTH * member_count


def _change_pct(current: float, proposed: float):
    """Percent change from current to proposed, safe when current is 0 or
    unknown (avoids ZeroDivisionError and meaningless ±inf%). Returns None when
    a percentage can't be computed so callers can render "n/a"."""
    if not current:
        return None
    return (proposed - current) / current * 100


def _observed_load(cache: CacheClient, cluster_id: str) -> dict:
    """Best-effort recent load context (last 1h avg of Active Average Sessions
    and CPU) to inform whether the proposed ACU range fits. WHY best-effort:
    the cache may be empty/unreachable and this is enrichment only, so any
    failure is swallowed and simply omits the context."""
    try:
        sql = """
            SELECT
                (SELECT AVG(value) FROM metric_snapshots
                 WHERE cluster_id = :cluster_id AND metric_type = 'aas'
                   AND ts > NOW() - MAKE_INTERVAL(hours => 1)) AS avg_aas,
                (SELECT AVG(value) FROM metric_snapshots
                 WHERE cluster_id = :cluster_id AND metric_type = 'cpu'
                   AND ts > NOW() - MAKE_INTERVAL(hours => 1)) AS avg_cpu
        """
        result = cache.execute(sql, {"cluster_id": cluster_id})
        row = result.rows[0] if result.rows else {}
        load = {}
        if row.get("avg_aas") is not None:
            load["avg_aas_1h"] = round(float(row["avg_aas"]), 2)
        if row.get("avg_cpu") is not None:
            load["avg_cpu_pct_1h"] = round(float(row["avg_cpu"]), 1)
        return load
    except Exception:  # pragma: no cover - enrichment is best-effort
        return {}


def simulate_scaling_impl(cache: CacheClient, cluster_id: str, new_min_acu: float = None, new_max_acu: float = None) -> dict:
    """Compare a cluster's CURRENT (live) Serverless v2 ACU range against a
    PROPOSED range and estimate the monthly cost delta.

    Behavior:
    - Reads the live cluster config via a cross-account-aware RDS client.
    - current min/max ACU come from ServerlessV2ScalingConfiguration; member
      counts (1 writer + N readers) come from DBClusterMembers.
    - Proposed range defaults to the current range when an argument is omitted.
    - If the engine is NOT Serverless v2, ACU scaling doesn't apply — surface
      the provisioned instance classes and skip cost math.
    - If the live describe fails (unregistered / cross-account / unreachable),
      fall back to cluster_meta for context and mark data_source as an estimate
      with current ACU unknown — never fabricate a 2-ACU figure.
    """
    try:
        rds = rds_client_for_cluster(cluster_id)
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        clusters = resp.get("DBClusters", [])
        cluster = clusters[0] if clusters else None
    except Exception as e:
        # Cross-account / unregistered / unreachable: degrade gracefully using
        # whatever the cache knows for context. Do NOT emit fake ACU numbers.
        meta = cache.execute(
            "SELECT * FROM cluster_meta WHERE cluster_id = :cluster_id",
            {"cluster_id": cluster_id},
        )
        cluster_meta = meta.rows[0] if meta.rows else {}
        return {
            "cluster_id": cluster_id,
            "current": {"min_acu": None, "max_acu": None},
            "proposed": {"min_acu": new_min_acu, "max_acu": new_max_acu},
            "cost_impact": {
                "current_monthly_estimate": None,
                "proposed_monthly_estimate": None,
                "change_pct": None,
            },
            "data_source": "estimate (live describe unavailable)",
            "note": (
                "라이브 클러스터 조회 실패로 현재 ACU 설정을 확인할 수 없습니다 "
                f"({type(e).__name__}). 비용 비교를 생략합니다. "
                f"캐시 컨텍스트: engine={cluster_meta.get('engine', 'unknown')}, "
                f"version={cluster_meta.get('engine_version', 'unknown')}."
            ),
        }

    if cluster is None:
        return {
            "cluster_id": cluster_id,
            "current": {"min_acu": None, "max_acu": None},
            "proposed": {"min_acu": new_min_acu, "max_acu": new_max_acu},
            "cost_impact": {
                "current_monthly_estimate": None,
                "proposed_monthly_estimate": None,
                "change_pct": None,
            },
            "data_source": "estimate (live describe unavailable)",
            "note": "describe_db_clusters가 해당 cluster_id를 반환하지 않았습니다. 비용 비교를 생략합니다.",
        }

    members = cluster.get("DBClusterMembers", [])
    writers = sum(1 for m in members if m.get("IsClusterWriter"))
    readers = sum(1 for m in members if not m.get("IsClusterWriter"))
    # Always bill at least one writer even if the API omitted member roles.
    member_count = max(1, writers + readers)

    scaling = cluster.get("ServerlessV2ScalingConfiguration")
    if not scaling:
        # Provisioned (non-Serverless-v2) cluster: ACU scaling is not a knob
        # here. Surface the instance classes so the DBA knows what to resize.
        instance_classes = [
            m.get("DBInstanceClass", "unknown")
            for m in members
            if m.get("DBInstanceClass")
        ]
        return {
            "cluster_id": cluster_id,
            "current": {"min_acu": None, "max_acu": None, "writers": writers, "readers": readers},
            "proposed": {"min_acu": new_min_acu, "max_acu": new_max_acu},
            "cost_impact": {
                "current_monthly_estimate": None,
                "proposed_monthly_estimate": None,
                "change_pct": None,
            },
            "instance_classes": instance_classes,
            "data_source": "live (describe_db_clusters)",
            "note": (
                "이 클러스터는 Aurora Serverless v2가 아닙니다(ServerlessV2ScalingConfiguration 없음). "
                "ACU 스케일링이 적용되지 않으며, 대신 프로비저닝된 인스턴스 클래스를 조정해야 합니다: "
                f"{', '.join(instance_classes) or 'unknown'}."
            ),
        }

    current_min = float(scaling.get("MinCapacity"))
    current_max = float(scaling.get("MaxCapacity"))
    # Proposed defaults to current when an argument is omitted, so an unchanged
    # axis produces a 0% delta rather than a spurious swing.
    proposed_min = float(new_min_acu) if new_min_acu is not None else current_min
    proposed_max = float(new_max_acu) if new_max_acu is not None else current_max

    current_cost = _midpoint_monthly_cost(current_min, current_max, member_count)
    proposed_cost = _midpoint_monthly_cost(proposed_min, proposed_max, member_count)
    change_pct = _change_pct(current_cost, proposed_cost)

    result = {
        "cluster_id": cluster_id,
        "current": {
            "min_acu": current_min,
            "max_acu": current_max,
            "writers": writers,
            "readers": readers,
        },
        "proposed": {"min_acu": proposed_min, "max_acu": proposed_max},
        "cost_impact": {
            "current_monthly_estimate": f"${current_cost:,.2f}",
            "proposed_monthly_estimate": f"${proposed_cost:,.2f}",
            "change_pct": round(change_pct, 1) if change_pct is not None else None,
        },
        "data_source": "live (describe_db_clusters)",
        "note": (
            f"중간값 ACU 기준 추정치(${ACU_PRICE_PER_HOUR}/ACU-hr × {HOURS_PER_MONTH}h, "
            f"{member_count}개 인스턴스). 리더는 라이터와 동일한 ACU 범위로 근사했고(API가 인스턴스별 "
            "설정을 노출하지 않음), 단가는 리전/IO-Optimized 여부에 따라 달라지는 근사값입니다. "
            "ACU 변경은 즉시 적용되며 다운타임이 없습니다."
        ),
    }

    observed = _observed_load(cache, cluster_id)
    if observed:
        result["observed_load"] = observed

    return result
