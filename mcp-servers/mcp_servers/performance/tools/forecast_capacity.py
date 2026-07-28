"""forecast_capacity: project when a metric hits its limit, with the limit
grounded in the CLUSTER'S real config (not a fleet-wide constant) and the trend
read from the metric series the cluster's engine ACTUALLY collects.

`metric` is a LOGICAL name and the engine family decides which metric_type and
which direction to read. That is the SAME vocabulary the REST dashboard endpoint
(api/dashboard/handler.py `_capacity_forecast`) takes, so the agent and the panel
answer the same question the same way for the same cluster. Before E1-5 the two
surfaces took different inputs (logical here, raw metric_type there) and the REST
family map had no rds_instance / elasticache key, so the dashboard said "not
applicable" while this tool produced an exhaustion ETA for the same cluster.
Logical won because the raw name is per-family (Aurora storage GROWS as
storage_bytes, standalone RDS storage DEPLETES as free_storage_bytes): a caller
that has to pick the raw name has to know the family first, and that is exactly
the mistake the `storage_gb` default made.

Logical name -> series, per family, every one verified against its writer:
  * storage      relational / documentdb -> storage_bytes (VolumeBytesUsed),
                 GROWING toward the 128 TiB volume ceiling
                 (cw_collector.py:5, docdb_cw_collector.py:10)
                 rds_instance -> free_storage_bytes (FreeStorageSpace),
                 DEPLETING toward 0 = STORAGE_FULL
                 (rds_instance_cw_collector.py:14)
  * connections  relational / documentdb / rds_instance -> db_connections
                 (cw_collector.py:7,26, docdb_cw_collector.py:14,
                 rds_instance_cw_collector.py:12)
  * aas          relational / rds_instance -> aas, written only by
                 pi_collector.py:5,25 (db.load.avg), so PI-capable families only
  * read_capacity / write_capacity
                 dynamodb -> consumed_rcu / consumed_wcu
                 (dynamodb_cw_collector.py:11,12,24,25), ceiling from the latest
                 provisioned_rcu / provisioned_wcu (:19,20) x 60s
  * memory       elasticache Redis/Valkey -> memory_usage_pct
                 (DatabaseMemoryUsagePercentage, elasticache_cw_collector.py:12).
                 Memcached is refused: that metric is NOT in
                 _MEMCACHED_METRICS (elasticache_cw_collector.py:28-41) and its
                 FreeableMemory is host memory, not cache fill, so there is no
                 honest substitute.

Families with no series for a logical metric are REFUSED
(status=unsupported_metric), never answered with a zero-sample forecast: the
history here is a default metric of `storage_gb`, which NO collector writes, so
every engine got zero samples reported as a flat trend.

Two response MODES, not one shape with a sign flip:
  * direction="up"   value grows toward a ceiling (storage_bytes, db_connections,
                     aas, consumed_*, memory_usage_pct)
  * direction="down" value shrinks toward a floor of 0 (free_storage_bytes).
`usage_pct` is computed HERE for both modes (0-100 or null) so no consumer has to
divide by `limit`, which is legitimately 0 in the "down" mode and 0/ungrounded
for an on-demand DynamoDB table.

An LRU/TTL cache sits pinned near maxmemory BY DESIGN, so its slope is about 0
and "days until 100%" is meaningless. When evictions occurred in the window the
cache is already recycling memory and status is `evicting` with NO date: the
accurate signal there is eviction volume, which elasticache_findings.py already
thresholds (EVICTIONS_WARNING = 100 per window). A cache with zero evictions and
a rising memory trend IS forecastable (that is the noeviction-policy case, where
filling up means write failures), and it keeps the ordinary "up" mode.

The linear slope (REGR_SLOPE) is kept but never reported as if it were precise:
we also compute the fit (REGR_R2) and sample count and turn them into a
``confidence`` plus a days-until RANGE. When the limit cannot be grounded in
real config we return ``grounded: False`` and NO date at all.

``status`` is on every payload: unknown_metric / unknown_cluster (bad input),
unsupported_metric (this engine has no such series), no_data (zero samples),
limit_reached (already there), evicting (cache recycling at capacity), ok.
"""

from mcp_servers.shared.cache_client import CacheClient
from mcp_servers.shared.engine_family import (
    DOCUMENTDB,
    DYNAMODB,
    ELASTICACHE,
    RDS_INSTANCE,
    RELATIONAL,
)
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

# Serverless v2는 instance_class가 db.serverless라 vCPU 토큰이 없다. 대신
# meta_collector가 채우는 serverlessv2_max_acu로 천장을 잡는다. ACU 1개는 약
# 2 GiB 메모리 + 대응 CPU라 vCPU 1개가 약 4 ACU다(db.r6g.large = 2 vCPU/16 GiB
# ≈ 8 ACU, db.r6g.4xlarge = 16 vCPU ≈ 64 ACU). AAS 천장은 vCPU 수이므로 max
# ACU를 vCPU로 환산해서 쓴다.
_ACU_PER_VCPU = 4.0

# 논리 이름 → 패밀리별 실제 metric_type. 매핑이 없는 패밀리는 그 시계열 자체가
# 없어 예측 불가로 거부한다(라이터는 모듈 docstring에 파일:줄로 적어 두었다).
_STORAGE_SERIES = {
    RELATIONAL: "storage_bytes",
    DOCUMENTDB: "storage_bytes",
    RDS_INSTANCE: "free_storage_bytes",
}

# db_connections(CloudWatch DatabaseConnections)를 쓰는 수집기는 cw_collector
# (relational) · docdb_cw_collector · rds_instance_cw_collector 셋뿐이다.
# DynamoDB에는 커넥션 개념이 없고(용량은 consumed_rcu/wcu), ElastiCache는
# curr_connections를 쓰지만 maxclients 천장을 수집하지 않아 한계를 근거 있게
# 잡을 수 없다. 매핑 없는 패밀리는 거부한다: 매핑이 없는 채로 진행하면 표본
# 0개를 "안정적, 한계 도달 없음"으로 보고한다.
_CONNECTION_SERIES = {
    RELATIONAL: "db_connections",
    DOCUMENTDB: "db_connections",
    RDS_INSTANCE: "db_connections",
}

# aas는 Performance Insights(pi_collector)만 쓴다 → PI를 켤 수 있는 패밀리
# (relational, rds_instance)뿐이다. DocumentDB는 PI가 없다(CAPABILITIES
# perf_insights=False).
_AAS_SERIES = {
    RELATIONAL: "aas",
    RDS_INSTANCE: "aas",
}

# DynamoDB 처리량. consumed_*는 분당 Sum이고 provisioned_*는 초당 rate라
# 천장은 provisioned × 60이다. provisioned_*는 billing_mode == PROVISIONED인
# 테이블에만 수집되므로(dynamodb_cw_collector.py:134) 온디맨드 테이블은 천장이
# 없다 → grounded=False(추세는 보고하되 날짜는 단정하지 않는다).
# (논리 이름, 패밀리) → (소비 series, 프로비저닝 series)
_THROUGHPUT_SERIES = {
    "read_capacity": {DYNAMODB: ("consumed_rcu", "provisioned_rcu")},
    "write_capacity": {DYNAMODB: ("consumed_wcu", "provisioned_wcu")},
}

# ElastiCache 메모리. memory_usage_pct(DatabaseMemoryUsagePercentage)는
# Redis/Valkey 목록에만 있고 Memcached 목록에는 없다. 퍼센트라 천장 100은
# 정의상 확정값이므로 node_type→메모리 맵(레포에 없음) 없이도 grounded다.
_MEMORY_SERIES = {ELASTICACHE: "memory_usage_pct"}
_MEMORY_LIMIT_PCT = 100.0

# 허용된 논리 메트릭 이름. 이름이 틀린 것(예: 옛 문서의 storage_gb)과 "이 엔진에는
# 그 시계열이 없음"은 전혀 다른 거부다: 전자를 엔진 탓으로 돌리면 에이전트가
# DynamoDB 거부와 구분하지 못한다.
_VALID_METRICS = (
    "storage", "connections", "aas", "read_capacity", "write_capacity", "memory",
)

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


def _is_memcached(engine) -> bool:
    return "memcached" in (engine or "").lower()


def _vcpu_for(instance_class: str):
    ic = (instance_class or "").lower()
    if not ic or "serverless" in ic:
        return None
    token = ic.rsplit(".", 1)[-1]
    return _VCPU_BY_SIZE.get(token)


def _latest_value(cache, sql: str, params: dict) -> float:
    """단일 컬럼(value) 1행 조회의 float. 없거나 숫자가 아니면 0.0 → 호출자가
    '근거 없음'으로 처리한다(폴백은 절대 grounded=True로 넘기지 않는다)."""
    rows = (cache.execute(sql, params).rows or [{}])
    return _f(rows[0].get("value"))


def _connections_limit(cache, cluster_id: str, fam: str, cluster: dict):
    """(limit, basis, grounded). 두 형제 구현과 동일한 우선순위:

      1. cluster_meta.max_connections. 이 컬럼을 쓰는 유일한 코드는
         api/clusters/seeder.py(데모 시더)다. meta_collector INSERT에는 이
         컬럼이 없으므로 실제 클러스터에서는 거의 항상 NULL이고, 그래서 이건
         힌트일 뿐 단독 근거가 될 수 없다.
      2. cluster_settings.max_connections 최신 행
         (data-pipeline/etl_collector/collectors/capacity_forecast.py와 동일
         쿼리). pg_locks / mysql_locks 수집기가 pg_settings ·
         performance_schema.global_variables에서 실제 값을 채운다.
      3. DocumentDB는 max_connections 설정이 없어 cluster_settings에 행이 없다.
         대신 DatabaseConnectionsLimit(db_connections_limit) 최신 관측값을
         천장으로 쓴다(api/dashboard/handler.py _capacity_forecast와 동일).

    아무것도 못 찾으면 grounded=False + 폴백값(날짜는 보고하지 않는다)."""
    mc = _f(cluster.get("max_connections"))
    if mc > 0:
        return mc, f"cluster_meta.max_connections={int(mc)}", True
    if fam == DOCUMENTDB:
        cl = _latest_value(
            cache,
            "SELECT value FROM metric_snapshots "
            "WHERE cluster_id = :cluster_id AND metric_type = 'db_connections_limit' "
            f"  {_CLUSTER_LEVEL_ONLY} "
            "ORDER BY ts DESC LIMIT 1",
            {"cluster_id": cluster_id},
        )
        if cl > 0:
            return cl, f"DatabaseConnectionsLimit 최신 관측값={int(cl)}", True
    else:
        cs = _latest_value(
            cache,
            "SELECT value FROM cluster_settings "
            "WHERE cluster_id = :cluster_id AND name = 'max_connections' "
            "ORDER BY updated_at DESC LIMIT 1",
            {"cluster_id": cluster_id},
        )
        if cs > 0:
            return cs, f"cluster_settings.max_connections={int(cs)}", True
    return float(_FALLBACK_CONNECTIONS), "max_connections 미상, 기본값 가정", False


def _aas_limit(cluster: dict):
    """(limit, basis, grounded). 프로비저닝 인스턴스는 instance_class의 vCPU,
    Serverless v2는 serverlessv2_max_acu(meta_collector가 채운다)를 vCPU로
    환산한다. 환산 없이 예전처럼 vCPU만 보면 db.serverless는 전부
    grounded=False라 Serverless v2 클러스터에는 영원히 날짜가 안 나왔다."""
    ic = cluster.get("instance_class")
    vcpu = _vcpu_for(ic)
    if vcpu:
        return float(vcpu), f"인스턴스 {ic} vCPU={vcpu} (AAS 포화 기준)", True
    # serverlessv2_max_acu는 Serverless v2 스케일링 설정이 있는 클러스터에만
    # 채워지고, 프로비저닝 인스턴스는 위 vCPU 경로에서 이미 끝난다.
    acu = _f(cluster.get("serverlessv2_max_acu"))
    if acu > 0:
        acu_vcpu = max(1.0, acu / _ACU_PER_VCPU)
        return (
            acu_vcpu,
            f"serverlessv2_max_acu={round(acu, 1)} → vCPU≈{round(acu_vcpu, 1)} (AAS 포화 기준)",
            True,
        )
    return float(_FALLBACK_AAS), "인스턴스 vCPU 미상(서버리스/미등록), 기본값 가정", False


def _throughput_limit(cache, cluster_id: str, provisioned_metric: str):
    """(limit, basis, grounded). consumed_*는 분당 Sum이므로 천장은 최신
    provisioned_* (초당 rate) × 60이다. 온디맨드 테이블은 provisioned_* 행이
    아예 없어(수집 조건이 billing_mode == PROVISIONED) 근거 있는 천장이 없다 →
    grounded=False. 추세 자체는 실데이터이므로 보고하고 날짜만 보류한다."""
    prov = _latest_value(
        cache,
        "SELECT value FROM metric_snapshots "
        "WHERE cluster_id = :cluster_id AND metric_type = :provisioned_metric "
        f"  {_CLUSTER_LEVEL_ONLY} "
        "ORDER BY ts DESC LIMIT 1",
        {"cluster_id": cluster_id, "provisioned_metric": provisioned_metric},
    )
    if prov > 0:
        return (
            prov * 60.0,
            f"{provisioned_metric} 최신값 {round(prov, 1)}/초 × 60초 = {int(prov * 60)}/분",
            True,
        )
    return 0.0, "온디맨드(프로비저닝 용량 없음), 근거 있는 천장 없음", False


def _evictions_in_window(cache, cluster_id: str, days_lookback: int) -> float:
    """조회 창 안의 eviction 총합. >0이면 캐시가 이미 메모리를 회수하며 돌고
    있다는 뜻이고, 그 상태의 "100% 도달까지 며칠"은 의미가 없다.
    evictions는 Redis/Valkey · Memcached 양쪽 목록에 다 있다
    (elasticache_cw_collector.py:18,33)."""
    return _latest_value(
        cache,
        "SELECT COALESCE(SUM(value), 0) AS value FROM metric_snapshots "
        "WHERE cluster_id = :cluster_id AND metric_type = 'evictions' "
        f"  {_CLUSTER_LEVEL_ONLY} "
        "  AND ts > NOW() - (:days_lookback || ' days')::interval",
        {"cluster_id": cluster_id, "days_lookback": days_lookback},
    )


def _resolve_series(cache, cluster_id: str, metric: str, fam: str, cluster: dict):
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
        metric_type = _CONNECTION_SERIES.get(fam)
        if metric_type is None:
            return None, 0.0, "", False
        return (metric_type, *_connections_limit(cache, cluster_id, fam, cluster))
    if metric == "aas":
        metric_type = _AAS_SERIES.get(fam)
        if metric_type is None:
            return None, 0.0, "", False
        return (metric_type, *_aas_limit(cluster))
    if metric in _THROUGHPUT_SERIES:
        pair = _THROUGHPUT_SERIES[metric].get(fam)
        if pair is None:
            return None, 0.0, "", False
        metric_type, provisioned_metric = pair
        return (metric_type, *_throughput_limit(cache, cluster_id, provisioned_metric))
    # metric == "memory"
    metric_type = _MEMORY_SERIES.get(fam)
    if metric_type is None or _is_memcached(cluster.get("engine")):
        # Memcached는 DatabaseMemoryUsagePercentage를 발행하지 않고
        # (elasticache_cw_collector.py의 _MEMCACHED_METRICS에 없음), FreeableMemory는
        # 캐시 적재량이 아니라 호스트 여유 메모리라 대체할 수 없다.
        return None, 0.0, "", False
    return metric_type, _MEMORY_LIMIT_PCT, "메모리 사용률 상한 100%", True


def _usage_pct(approach_down: bool, current: float, limit: float, alloc_gb: float):
    """0-100 사용률 또는 None. 소비자가 limit으로 나누지 않게 서버에서 계산한다:
    감소 모드의 limit은 정당하게 0이고(여유 0바이트 = 소진) 온디맨드 DynamoDB는
    천장 자체가 없어서, (current/limit)*100은 0으로 나누거나 무의미한 퍼센트를
    만든다. 감소 모드의 사용률은 한계가 아니라 할당량 대비로만 정의된다."""
    if approach_down:
        if alloc_gb > 0:
            total = alloc_gb * 1024 ** 3
            return round(max(0.0, min(100.0, (total - current) / total * 100)), 1)
        return None
    if limit > 0:
        return round(max(0.0, min(100.0, current / limit * 100)), 1)
    return None


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
        "SELECT engine, max_connections, instance_class, serverlessv2_max_acu, "
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
    metric_type, limit, limit_basis, grounded = _resolve_series(
        cache, cluster_id, metric, fam, cluster)
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
            "reason": (
                f"{fam} 엔진(engine={cluster.get('engine') or '미상'})에서는 {metric} "
                f"시계열이 수집되지 않아 예측할 수 없습니다."
                + (" Memcached는 DatabaseMemoryUsagePercentage를 발행하지 않습니다."
                   if metric == "memory" and fam == ELASTICACHE else "")
            ),
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

    # free_storage_bytes만 값이 내려가면서 한계(여유 0)에 닿는다. 나머지 메트릭은
    # 올라가면서 천장에 닿는다. "이미 도달"과 "한계에서 멀어지는 추세"를 가르는
    # 기준이 이 접근 방향이고, 소비자가 막대/사용률을 그리는 기준도 이것이다.
    approach_down = metric_type == "free_storage_bytes"

    def _days(s):
        # 방향 무관: gap/slope가 양수일 때만 한계로 접근 중이다. 증가 메트릭은
        # gap>0 & slope>0, 감소(free_storage_bytes)는 gap<0 & slope<0.
        # 접근 중이 아니면 None(센티넬 -1을 쓰면 에이전트가 "-1일 후 한계 도달"로
        # 그대로 읽어 쓴다). 하루 미만은 0이 아니라 1로 올린다(가장 급한 케이스).
        if not s:
            return None
        d = (limit - current) / s
        return max(1, int(d)) if d > 0 else None

    # 이미 한계에 도달(또는 초과)했는가. _days는 gap<=0을 전부 None으로 뭉개므로,
    # 커넥션 상한에 붙은 클러스터와 여유 0바이트 인스턴스가 "추세가 한계로 향하지
    # 않습니다 / approaching_limit=false / confidence=low"라는 가장 안심되는 응답을
    # 받아 왔다. 가장 급한 상태에 가장 조용한 payload였다.
    # 표본이 있어야 한다: 표본 0개면 current_value는 조회 창 밖의 낡은 값이거나
    # 행이 아예 없어 0.0인데, 여유 스토리지(한계 0)에서는 그 0.0이 "소진"으로
    # 읽혀 데이터 없는 클러스터에 STORAGE_FULL 경보를 낸다.
    at_limit = (
        grounded
        and n > 0
        and row.get("current_value") is not None
        and (current <= limit if approach_down else current >= limit)
    )

    # LRU/TTL 캐시는 설계상 maxmemory 근처에 붙어 있어 기울기가 0에 가깝고
    # "며칠 후 100%"가 무의미하다. eviction이 실제로 발생했다면 캐시는 이미
    # 메모리를 회수하며 돌고 있으므로 ETA 대신 그 사실을 보고한다. eviction이
    # 0인데 메모리가 오르는 캐시는 진짜 채워지는 중(noeviction 정책)이라 일반
    # 증가 모드를 그대로 쓴다.
    evicting = (
        metric == "memory" and n > 0
        and _evictions_in_window(cache, cluster_id, days_lookback) > 0
    )

    # 한계를 실제 설정에서 확인하지 못하면 날짜를 단정하지 않는다.
    days_until = 0 if at_limit else (_days(slope) if grounded else None)
    # approaching_limit은 "지금 조치가 필요한가"를 뜻해야 한다. 기울기가 한계
    # 방향이면 얼마나 멀든 True로 두면, 라이브에서 실제로 나온 것처럼
    # days_until_limit=80170(약 219년)에 approaching_limit=true가 붙는다. DBA는
    # 그 불리언만 보고 조사에 들어간다. 그래서 ETA 자체는 정직하게 그대로 두고,
    # 플래그만 실행 가능한 기간으로 한정한다.
    # 추세가 한계로 향하는가(방향)와, 지금 조치할 사안인가(기간)는 별개다.
    # confidence와 불확실성 밴드는 "방향"에 걸어야 한다: 적합도가 좋은 먼 미래
    # 추정은 신뢰도 낮은 추정이 아니라 신뢰도 높은 먼 미래 추정이다.
    heading_to_limit = days_until is not None and not at_limit
    approaching = at_limit or (heading_to_limit and days_until <= _ACTIONABLE_HORIZON_DAYS)
    beyond_horizon = heading_to_limit and not approaching
    if evicting:
        # eviction 중인 캐시의 ETA는 보고하지 않는다. 그리고 정상 LRU 캐시를
        # 매번 경보로 올리지도 않는다(그게 "건강한 캐시를 소진 임박으로 읽는"
        # 오답이다). 정확한 신호는 eviction 양이고, 그건 이미
        # elasticache_findings.py가 임계로 관리한다.
        days_until = None
        heading_to_limit = False
        approaching = False
        beyond_horizon = False
    # Confidence from fit + samples; a poor fit / thin data widens the band and
    # lowers confidence so the number isn't mistaken for precision.
    if at_limit or evicting:
        # 외삽이 아니라 관측된 상태다. 적합도가 나빠도 "이미 한계"는 사실이므로
        # 적합도 기반 등급을 그대로 쓰면 가장 확실한 사실이 low로 내려간다.
        confidence = "high"
    elif not grounded or n < 20 or not heading_to_limit:
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

    if n == 0:
        note = (
            f"최근 {days_lookback}일 동안 {metric_type} 표본이 없어 추세를 계산할 수 없습니다"
            f"(수집 미시작이거나 이 클러스터에서 해당 메트릭이 올라오지 않음). 표본 0개는 "
            f"'안정적(stable)'이 아니라 status=no_data, forecast=no_data입니다."
        )
    elif evicting:
        note = (
            f"최근 {days_lookback}일 동안 eviction이 발생했습니다. eviction 정책(LRU/TTL)이 "
            f"걸린 캐시는 설계상 메모리 상한 근처에서 동작하므로 '100% 도달까지 며칠'은 "
            f"의미가 없습니다(현재 {round(current, 1)}%, 기울기 {round(slope, 4)}/일, "
            f"표본 {n}개). 정확한 신호는 eviction 양과 hit rate이며 "
            f"elasticache_evictions_spike · elasticache_memory_pressure finding이 이를 "
            f"임계로 관리합니다. days_until_limit=null, approaching_limit=false는 "
            f"'문제 없음'이 아니라 '이 지표로는 소진 시점을 말할 수 없음'입니다."
        )
    elif at_limit:
        note = (
            f"한계값 기준: {limit_basis}. 현재값이 이미 한계에 도달했거나 넘었습니다"
            f"(현재 {round(current, 2)}, 한계 {round(limit, 2)}). 예측이 아니라 관측된 상태이므로 "
            f"days_until_limit=0, approaching_limit=true입니다. 즉시 조치가 필요합니다."
            + (" 여유 스토리지 0바이트는 쓰기 중단(STORAGE_FULL)을 의미합니다."
               if approach_down else "")
            + f" (기울기 {round(slope, 4)}/일, 표본 {n}개)"
        )
    elif not grounded:
        note = (
            f"한계값을 클러스터 실제 설정에서 확인할 수 없어({limit_basis}) 도달 시점을 단정하지 "
            f"않습니다. 추세만 참고하세요(기울기 {round(slope, 4)}/일, 표본 {n}개)."
        )
    elif approaching:
        note = (
            f"한계값 기준: {limit_basis}. 선형 외삽은 현재 추세가 유지된다고 가정합니다(R²={round(r2, 2)}, "
            f"표본 {n}개). days_until_limit은 점 추정이며 range는 추세 적합도 기반 불확실성 밴드입니다."
        )
    elif beyond_horizon:
        note = (
            f"한계값 기준: {limit_basis}. 추세는 한계로 향하지만 도달 시점이 약 {days_until}일 "
            f"({round(days_until / 365.0, 1)}년) 뒤로, 실행 가능 기간 {_ACTIONABLE_HORIZON_DAYS}일을 "
            f"넘습니다. 지금 조치할 사안이 아니라 approaching_limit=false입니다"
            f"(기울기 {round(slope, 4)}/일, 표본 {n}개)."
        )
    else:
        note = (
            f"한계값 기준: {limit_basis}. 현재 추세(기울기 {round(slope, 4)}/일, 표본 {n}개)는 한계로 "
            f"향하지 않아 도달 시점이 없습니다(days_until_limit=null, approaching_limit=false)."
        )

    alloc = _f(cluster.get("allocated_storage_gb"))
    result = {
        "cluster_id": cluster_id,
        "metric": metric,
        "metric_type": metric_type,
        "engine_family": fam,
        # 성공 경로도 거부 경로와 같은 status 키를 갖는다: no_data(표본 0개) ·
        # evicting(캐시가 상한 근처에서 eviction 중) · limit_reached(이미 도달) · ok.
        "status": (
            "no_data" if n == 0
            else "evicting" if evicting
            else "limit_reached" if at_limit
            else "ok"
        ),
        "current_value": current,
        "limit": limit,
        "limit_basis": limit_basis,
        "grounded": grounded,
        # 두 응답 모드: up = 천장으로 증가, down = 0으로 소진. 소비자는 이 값으로
        # 라벨과 막대를 고르고, 퍼센트는 usage_pct를 그대로 쓴다(직접 나누면
        # limit=0인 감소 모드와 천장 없는 온디맨드에서 깨진다).
        "direction": "down" if approach_down else "up",
        "usage_pct": _usage_pct(approach_down, current, limit, alloc),
        "slope_per_day": round(slope, 4),
        "r2": round(r2, 3),
        "samples": n,
        # days_until_limit: 0(status=limit_reached, 이미 도달) · 양의 정수(추세가
        # 한계로 향함) · null. null의 이유(추세가 한계로 향하지 않음 / 한계 근거
        # 없음 / eviction 중)는 note에 문장으로 남긴다. 양의 정수여도 실행 가능
        # 기간을 넘으면 approaching_limit는 false다.
        "days_until_limit": days_until,
        "approaching_limit": approaching,
        "days_until_limit_range": days_range,
        "confidence": confidence,
        # free_storage_bytes가 줄어드는 것은 소진(가장 위험)이므로 'shrinking'처럼
        # 안심되는 단어를 붙이지 않는다. 표본 0개는 추세가 아니므로 'stable'로
        # 라벨하지 않는다("안정적"으로 읽히는 거짓 안심).
        "forecast": (
            "no_data" if n == 0
            else "growing" if slope > 0
            else "stable" if slope == 0
            else "depleting" if approach_down
            else "shrinking"
        ),
        "note": note,
    }
    if approach_down and alloc > 0:
        # 할당 용량이 있으면 사용률 컨텍스트를 붙인다(collector와 동일 계산).
        result["allocated_gb"] = alloc
    return result
