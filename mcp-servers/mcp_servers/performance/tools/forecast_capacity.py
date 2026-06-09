"""forecast_capacity — project when a metric hits its limit, with the limit
grounded in the CLUSTER'S real config (not a fleet-wide constant).

The old version compared a linear trend against hardcoded limits
(connections=5000, aas=64, storage_gb=128000) for EVERY cluster — wrong for a
db.r6g.large (≈2 vCPU, a few hundred max_connections). Now:

  * connections → the cluster's real ``max_connections`` (cluster_meta).
  * aas        → the instance's vCPU count (sustained AAS > vCPU = CPU
                 saturation); derived from ``instance_class``.
  * storage_gb → Aurora's actual volume ceiling (128 TiB).

The linear slope (REGR_SLOPE) is kept but no longer reported as if it were
precise: we also compute the regression fit (REGR_R2) and sample count and turn
them into a ``confidence`` plus a days-until RANGE, because extrapolating a
noisy trend to a hard limit is inherently uncertain.
"""

from mcp_servers.shared.cache_client import CacheClient

# Aurora cluster volume ceiling (128 TiB) — a real platform limit, not a guess.
_AURORA_MAX_STORAGE_GB = 131072
# vCPU by instance size token — AAS saturates around the vCPU count.
# (t3/t4g.medium = 2 vCPU; r/m-class starts at large = 2 vCPU.)
_VCPU_BY_SIZE = {
    "medium": 2, "large": 2, "xlarge": 4, "2xlarge": 8, "4xlarge": 16,
    "8xlarge": 32, "12xlarge": 48, "16xlarge": 64, "24xlarge": 96, "32xlarge": 128,
}
# Last-resort fallbacks when the cluster's real config is unknown (flagged low
# confidence + noted, never silently authoritative).
_FALLBACK_CONNECTIONS = 5000
_FALLBACK_AAS = 64


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _vcpu_for(instance_class: str):
    ic = (instance_class or "").lower()
    if not ic or "serverless" in ic:
        return None
    token = ic.rsplit(".", 1)[-1]
    return _VCPU_BY_SIZE.get(token)


def _resolve_limit(metric: str, cluster: dict) -> tuple[float, str, bool]:
    """(limit, basis, grounded) for the metric, from the cluster's real config."""
    if metric == "storage_gb":
        return float(_AURORA_MAX_STORAGE_GB), "Aurora 볼륨 상한 128 TiB", True
    if metric == "connections":
        mc = cluster.get("max_connections")
        if mc:
            return _f(mc), f"cluster_meta.max_connections={int(_f(mc))}", True
        return float(_FALLBACK_CONNECTIONS), "max_connections 미상 — 기본값 가정", False
    if metric == "aas":
        vcpu = _vcpu_for(cluster.get("instance_class"))
        if vcpu:
            return float(vcpu), f"인스턴스 {cluster.get('instance_class')} vCPU={vcpu} (AAS 포화 기준)", True
        return float(_FALLBACK_AAS), "인스턴스 vCPU 미상(서버리스/미등록) — 기본값 가정", False
    return 1000.0, "알 수 없는 메트릭 — 기본 한계 1000", False


def forecast_capacity_impl(
    cache: CacheClient,
    cluster_id: str,
    metric: str = "storage_gb",
    days_lookback: int = 30,
) -> dict:
    # Trend + fit + sample count over the lookback. current = latest reading
    # (not MAX, which would overstate "current" for a bouncy metric like
    # connections). REGR_R2 gives how linear the trend actually is.
    sql = """
        SELECT
            REGR_SLOPE(value, EXTRACT(EPOCH FROM ts) / 86400) AS slope_per_day,
            REGR_R2(value, EXTRACT(EPOCH FROM ts) / 86400) AS r2,
            COUNT(*) AS n,
            (SELECT value FROM metric_snapshots m2
             WHERE m2.cluster_id = :cluster_id AND m2.metric_type = :metric
             ORDER BY ts DESC LIMIT 1) AS current_value
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id AND metric_type = :metric
          AND ts > NOW() - MAKE_INTERVAL(days => :days_lookback)
    """
    params = {"cluster_id": cluster_id, "metric": metric, "days_lookback": days_lookback}
    row = (cache.execute(sql, params).rows or [{}])[0]
    slope = _f(row.get("slope_per_day"))
    r2 = _f(row.get("r2"))
    n = int(_f(row.get("n")))
    current = _f(row.get("current_value"))

    meta = cache.execute(
        "SELECT max_connections, instance_class FROM cluster_meta WHERE cluster_id = :cluster_id",
        {"cluster_id": cluster_id},
    )
    cluster = meta.rows[0] if meta.rows else {}
    limit, limit_basis, grounded = _resolve_limit(metric, cluster)

    def _days(s):
        return int((limit - current) / s) if s and s > 0 else -1

    days_until = _days(slope)
    # Confidence from fit + samples; a poor fit / thin data widens the band and
    # lowers confidence so the number isn't mistaken for precision.
    if not grounded or n < 20 or slope <= 0:
        confidence = "low"
    elif r2 >= 0.7 and n >= 100:
        confidence = "high"
    elif r2 >= 0.4:
        confidence = "medium"
    else:
        confidence = "low"

    # Days-until band: slope uncertainty scales inversely with fit (R²). A
    # better fit → tighter band around the point estimate.
    days_range = None
    if slope > 0 and days_until > 0:
        spread = max(0.15, 1.0 - max(0.0, min(r2, 1.0)))  # 0.15 (great fit) .. 1.0 (no fit)
        low = _days(slope * (1.0 + spread))   # faster growth → sooner
        high = _days(slope * (1.0 - spread)) if spread < 1.0 else -1  # slower → later (or never)
        days_range = [low, high]

    return {
        "cluster_id": cluster_id,
        "metric": metric,
        "current_value": current,
        "limit": limit,
        "limit_basis": limit_basis,
        "slope_per_day": round(slope, 4),
        "r2": round(r2, 3),
        "samples": n,
        "days_until_limit": days_until,
        "days_until_limit_range": days_range,
        "confidence": confidence,
        "forecast": "growing" if slope > 0 else "stable" if slope == 0 else "shrinking",
        "note": (
            f"한계값 기준: {limit_basis}. 선형 외삽은 현재 추세가 유지된다고 가정합니다(R²={round(r2, 2)}, "
            f"표본 {n}개). days_until은 점 추정이며 range는 추세 적합도 기반 불확실성 밴드입니다."
        ),
    }
