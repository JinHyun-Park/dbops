"""forecast_capacity: project when a metric hits its limit, with the limit
grounded in the CLUSTER'S real config (not a fleet-wide constant) and the trend
read from the metric series the cluster's engine ACTUALLY collects.

Two historical wrong-answer paths, both fixed here:

  1. hardcoded limits (connections=5000, aas=64, storage_gb=128000) applied to
     every cluster, wrong for a db.r6g.large. Limits now come from
     cluster_meta (max_connections / instance_class / allocated_storage_gb).
  2. the DEFAULT metric was `storage_gb`, which NO collector ever writes, so
     the default path returned zero samples for every engine and reported it as
     a flat trend. The real series are:
       * Aurora / DocumentDB: `storage_bytes` (VolumeBytesUsed), GROWING
         toward the 128 TiB volume ceiling.
       * standalone RDS instance: `free_storage_bytes` (FreeStorageSpace),
         SHRINKING toward 0 (disk exhaustion → STORAGE_FULL).
       * connections: `db_connections` (CloudWatch DatabaseConnections,
         collected for every engine). The PI-only `connections` series is empty
         whenever Performance Insights is off.
     `metric` is therefore a LOGICAL name (storage/connections/aas) and the
     engine family decides which series and which direction to read. Same math
     as data-pipeline/etl_collector/collectors/capacity_forecast.py.

The linear slope (REGR_SLOPE) is kept but never reported as if it were precise:
we also compute the fit (REGR_R2) and sample count and turn them into a
``confidence`` plus a days-until RANGE. When the limit cannot be grounded in
real config we return ``grounded: False`` and NO date at all.
"""

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.engine_family import DOCUMENTDB, RDS_INSTANCE, RELATIONAL
from mcp_servers.shared.engine_family import engine_family as _engine_family
from mcp_servers.shared.metric_filters import CLUSTER_LEVEL_ONLY

# Aurora 클러스터 볼륨 상한(128 TiB), 추정이 아닌 실제 플랫폼 한계.
# DocumentDB는 엔진 8.0 미만 인스턴스 기반 클러스터가 128 TiB, 8.0 이상은
# 256 TiB이므로 128 TiB는 "정확"이 아니라 보수적으로 일찍 경고하는 값이다
# (8.0 클러스터에는 실제 한계의 절반에서 알린다).
# storage_bytes는 바이트 단위라 한계도 바이트로 둔다(대시보드 _CAPACITY_METRICS,
# capacity_forecast collector와 같은 상수).
_VOLUME_MAX_BYTES = 128 * 1024 ** 4
# vCPU by instance size token: AAS saturates around the vCPU count.
# (t3/t4g.medium = 2 vCPU; r/m-class starts at large = 2 vCPU.)
_VCPU_BY_SIZE = {
    "medium": 2, "large": 2, "xlarge": 4, "2xlarge": 8, "4xlarge": 16,
    "8xlarge": 32, "12xlarge": 48, "16xlarge": 64, "24xlarge": 96, "32xlarge": 128,
}
# Last-resort fallbacks when the cluster's real config is unknown (flagged
# grounded=False + no date, never silently authoritative).
_FALLBACK_CONNECTIONS = 5000
_FALLBACK_AAS = 64

# 논리 스토리지 메트릭 → 패밀리별 실제 metric_type. 매핑이 없는 패밀리
# (DynamoDB/ElastiCache)는 스토리지 시계열 자체가 없어 예측 불가로 거부한다.
_STORAGE_SERIES = {
    RELATIONAL: "storage_bytes",
    DOCUMENTDB: "storage_bytes",
    RDS_INSTANCE: "free_storage_bytes",
}

# 허용된 논리 메트릭 이름. 이름이 틀린 것(예: 옛 문서의 storage_gb)과 "이 엔진에는
# 그 시계열이 없음"은 전혀 다른 거부다: 전자를 엔진 탓으로 돌리면 에이전트가
# DynamoDB 거부와 구분하지 못한다.
_VALID_METRICS = ("storage", "connections", "aas")

# metric_snapshots는 같은 metric_type을 여러 차원으로 저장한다(인스턴스별·PI
# 대기이벤트별·GSI별). 클러스터 단위 회귀는 반드시 strict 필터를 써야 한다.
# 이유와 반례는 shared/metric_filters.py 주석 참고.
_CLUSTER_LEVEL_ONLY = CLUSTER_LEVEL_ONLY

# approaching_limit을 붙일 실행 가능 기간. 라이브에서 rds_instance 여유 스토리지가
# 하루 수 MB씩만 줄어 ETA가 약 219년으로 나왔는데 플래그는 true였다. 1년을 넘는
# 도달 시점은 용량 계획 대상이지 지금 조치할 알림이 아니다. ETA 숫자는 그대로
# 보고하고 플래그만 이 기간으로 제한한다.
_ACTIONABLE_HORIZON_DAYS = 365


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


def _resolve_series(metric: str, fam: str, cluster: dict):
    """(metric_type, limit, basis, grounded). metric_type=None → 이 엔진에서
    예측 불가(수집되는 시계열이 없음). metric 이름은 호출자가 _VALID_METRICS로
    이미 검증했으므로 여기서 이름 오류는 다루지 않는다."""
    if metric == "storage":
        metric_type = _STORAGE_SERIES.get(fam)
        if metric_type is None:
            return None, 0.0, "", False
        if metric_type == "free_storage_bytes":
            # 여유 공간이 0으로 줄어드는 소진 ETA. 한계 0은 하드 플로어라
            # (0 bytes = STORAGE_FULL) 항상 grounded다. allocated_storage_gb는
            # 사용률 컨텍스트용이며 없어도 ETA 자체는 유효하다.
            # 단, RDS 스토리지 오토스케일링(MaxAllocatedStorage)이 켜져 있으면
            # 0에 닿는 대신 볼륨이 자동 확장되어 ETA가 지나 보수적일 수 있다.
            # MaxAllocatedStorage는 현재 resource_details에 수집하지 않으므로
            # 오토스케일링 여부를 여기서 알 수 없다.
            alloc = _f(cluster.get("allocated_storage_gb"))
            basis = "여유 스토리지 소진(0 bytes)"
            if alloc > 0:
                basis += f", 할당 {int(alloc)}GB"
            return metric_type, 0.0, basis, True
        return metric_type, float(_VOLUME_MAX_BYTES), "클러스터 볼륨 상한 128 TiB", True
    if metric == "connections":
        mc = cluster.get("max_connections")
        if mc:
            return "db_connections", _f(mc), f"cluster_meta.max_connections={int(_f(mc))}", True
        return "db_connections", float(_FALLBACK_CONNECTIONS), "max_connections 미상, 기본값 가정", False
    # metric == "aas"
    vcpu = _vcpu_for(cluster.get("instance_class"))
    if vcpu:
        return "aas", float(vcpu), f"인스턴스 {cluster.get('instance_class')} vCPU={vcpu} (AAS 포화 기준)", True
    return "aas", float(_FALLBACK_AAS), "인스턴스 vCPU 미상(서버리스/미등록), 기본값 가정", False


def forecast_capacity_impl(
    cache: CacheClient,
    cluster_id: str,
    metric: str = "storage",
    days_lookback: int = 30,
) -> dict:
    if metric not in _VALID_METRICS:
        return {
            "cluster_id": cluster_id,
            "metric": metric,
            "status": "unknown_metric",
            "samples": 0,
            "days_until_limit": None,
            "approaching_limit": False,
            "grounded": False,
            "reason": (
                f"'{metric}' 는 지원하지 않는 메트릭 이름입니다. "
                f"사용 가능한 값: {', '.join(_VALID_METRICS)}."
            ),
        }

    # 엔진 패밀리와 실제 한계를 먼저 읽는다. 어떤 metric_type을 조회할지가
    # 패밀리에 달려 있다(Aurora storage_bytes vs RDS free_storage_bytes).
    meta = cache.execute(
        "SELECT engine, max_connections, instance_class, "
        "       resource_details->>'allocated_storage_gb' AS allocated_storage_gb "
        "FROM cluster_meta WHERE cluster_id = :cluster_id",
        {"cluster_id": cluster_id},
    )
    if not meta.rows:
        # FAIL-CLOSED: cluster_meta 행이 없으면 패밀리를 해석하지 않는다.
        # engine_family(None)은 legacy 기본값으로 relational을 돌려주므로, 이대로
        # 진행하면 미등록 클러스터에 storage_bytes/128 TiB를 적용해 표본 0개를
        # "안정적, 한계 도달 없음"으로 보고한다. 그게 바로 거짓 안심 경로다.
        return {
            "cluster_id": cluster_id,
            "metric": metric,
            "status": "unknown_cluster",
            "samples": 0,
            "days_until_limit": None,
            "approaching_limit": False,
            "grounded": False,
            "reason": (
                "cluster_meta에 이 클러스터가 없습니다(미등록이거나 첫 메트릭 수집 전). "
                "등록 및 수집 상태를 확인한 뒤 다시 예측하세요."
            ),
        }
    cluster = meta.rows[0]
    fam = _engine_family(cluster.get("engine"))
    metric_type, limit, limit_basis, grounded = _resolve_series(metric, fam, cluster)
    if metric_type is None:
        return {
            "cluster_id": cluster_id,
            "metric": metric,
            "engine_family": fam,
            "status": "unsupported_metric",
            "samples": 0,
            "days_until_limit": None,
            "approaching_limit": False,
            "grounded": False,
            "reason": f"{fam} 엔진에서는 {metric} 시계열이 수집되지 않아 예측할 수 없습니다.",
        }

    # Trend + fit + sample count over the lookback. current = latest reading
    # (not MAX, which would overstate "current" for a bouncy metric like
    # connections). REGR_R2 gives how linear the trend actually is.
    # _CLUSTER_LEVEL_ONLY는 집계 WHERE와 current_value 서브셀렉트 **양쪽에** 필수다
    # (이유는 상수 정의부 주석 참고). 한쪽만 걸면 회귀는 깨끗해도 "현재값"이
    # 인스턴스 행일 수 있다.
    sql = f"""
        SELECT
            REGR_SLOPE(value, EXTRACT(EPOCH FROM ts) / 86400) AS slope_per_day,
            REGR_R2(value, EXTRACT(EPOCH FROM ts) / 86400) AS r2,
            COUNT(*) AS n,
            (SELECT value FROM metric_snapshots m2
             WHERE m2.cluster_id = :cluster_id AND m2.metric_type = :metric
               {_CLUSTER_LEVEL_ONLY}
             ORDER BY ts DESC LIMIT 1) AS current_value
        FROM metric_snapshots
        WHERE cluster_id = :cluster_id AND metric_type = :metric
          {_CLUSTER_LEVEL_ONLY}
          AND ts > NOW() - (:days_lookback || ' days')::interval
    """
    params = {"cluster_id": cluster_id, "metric": metric_type, "days_lookback": days_lookback}
    row = (cache.execute(sql, params).rows or [{}])[0]
    slope = _f(row.get("slope_per_day"))
    r2 = _f(row.get("r2"))
    n = int(_f(row.get("n")))
    current = _f(row.get("current_value"))

    def _days(s):
        # 방향 무관: gap/slope가 양수일 때만 한계로 접근 중이다. 증가 메트릭은
        # gap>0 & slope>0, 감소(free_storage_bytes)는 gap<0 & slope<0.
        # 접근 중이 아니면 None(센티넬 -1을 쓰면 에이전트가 "-1일 후 한계 도달"로
        # 그대로 읽어 쓴다). 하루 미만은 0이 아니라 1로 올린다(가장 급한 케이스).
        if not s:
            return None
        d = (limit - current) / s
        return max(1, int(d)) if d > 0 else None

    # 한계를 실제 설정에서 확인하지 못하면 날짜를 단정하지 않는다.
    days_until = _days(slope) if grounded else None
    # approaching_limit은 "지금 조치가 필요한가"를 뜻해야 한다. 기울기가 한계
    # 방향이면 얼마나 멀든 True로 두면, 라이브에서 실제로 나온 것처럼
    # days_until_limit=80170(약 219년)에 approaching_limit=true가 붙는다. DBA는
    # 그 불리언만 보고 조사에 들어간다. 그래서 ETA 자체는 정직하게 그대로 두고,
    # 플래그만 실행 가능한 기간으로 한정한다.
    # 추세가 한계로 향하는가(방향)와, 지금 조치할 사안인가(기간)는 별개다.
    # confidence와 불확실성 밴드는 "방향"에 걸어야 한다: 적합도가 좋은 먼 미래
    # 추정은 신뢰도 낮은 추정이 아니라 신뢰도 높은 먼 미래 추정이다.
    heading_to_limit = days_until is not None
    approaching = heading_to_limit and days_until <= _ACTIONABLE_HORIZON_DAYS
    beyond_horizon = heading_to_limit and not approaching
    # Confidence from fit + samples; a poor fit / thin data widens the band and
    # lowers confidence so the number isn't mistaken for precision.
    if not grounded or n < 20 or not heading_to_limit:
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
    if heading_to_limit:
        spread = max(0.15, 1.0 - max(0.0, min(r2, 1.0)))  # 0.15 (great fit) .. 1.0 (no fit)
        low = _days(slope * (1.0 + spread))   # 더 빠른 추세 → 더 이르게
        # 더 느린 추세 → 더 늦게, 또는 아예 도달 안 함(null)
        high = _days(slope * (1.0 - spread)) if spread < 1.0 else None
        days_range = [low, high]

    result = {
        "cluster_id": cluster_id,
        "metric": metric,
        "metric_type": metric_type,
        "engine_family": fam,
        "current_value": current,
        "limit": limit,
        "limit_basis": limit_basis,
        "grounded": grounded,
        "slope_per_day": round(slope, 4),
        "r2": round(r2, 3),
        "samples": n,
        # days_until_limit는 두 값만 가진다: 정수(추세가 한계로 향함) 또는 null.
        # null의 이유(추세가 한계로 향하지 않음 / 한계 근거 없음)는 note에 문장으로
        # 남긴다. 정수여도 실행 가능 기간을 넘으면 approaching_limit는 false다.
        "days_until_limit": days_until,
        "approaching_limit": approaching,
        "days_until_limit_range": days_range,
        "confidence": confidence,
        # free_storage_bytes가 줄어드는 것은 소진(가장 위험)이므로 'shrinking'처럼
        # 안심되는 단어를 붙이지 않는다.
        "forecast": (
            "growing" if slope > 0
            else "stable" if slope == 0
            else "depleting" if metric_type == "free_storage_bytes"
            else "shrinking"
        ),
        "note": (
            f"한계값을 클러스터 실제 설정에서 확인할 수 없어({limit_basis}) 도달 시점을 단정하지 "
            f"않습니다. 추세만 참고하세요(기울기 {round(slope, 4)}/일, 표본 {n}개)."
            if not grounded else
            f"한계값 기준: {limit_basis}. 선형 외삽은 현재 추세가 유지된다고 가정합니다(R²={round(r2, 2)}, "
            f"표본 {n}개). days_until_limit은 점 추정이며 range는 추세 적합도 기반 불확실성 밴드입니다."
            if approaching else
            f"한계값 기준: {limit_basis}. 추세는 한계로 향하지만 도달 시점이 약 {days_until}일 "
            f"({round(days_until / 365.0, 1)}년) 뒤로, 실행 가능 기간 {_ACTIONABLE_HORIZON_DAYS}일을 "
            f"넘습니다. 지금 조치할 사안이 아니라 approaching_limit=false입니다"
            f"(기울기 {round(slope, 4)}/일, 표본 {n}개)."
            if beyond_horizon else
            f"한계값 기준: {limit_basis}. 현재 추세(기울기 {round(slope, 4)}/일, 표본 {n}개)는 한계로 "
            f"향하지 않아 도달 시점이 없습니다(days_until_limit=null, approaching_limit=false)."
        ),
    }
    if metric_type == "free_storage_bytes":
        # 할당 용량이 있으면 사용률 컨텍스트를 붙인다(collector와 동일 계산).
        alloc = _f(cluster.get("allocated_storage_gb"))
        if alloc > 0:
            total = alloc * 1024 ** 3
            result["allocated_gb"] = alloc
            result["usage_pct"] = round(max(0.0, (total - current) / total * 100), 1)
    return result
