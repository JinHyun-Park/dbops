"""MySQL Parameter Fitness: 이 MySQL 클러스터/인스턴스의 실측 워크로드 기준
파라미터 적정성 진단(pg_param_fitness의 MySQL 대응).

두 패밀리에서 돈다: Aurora MySQL(relational)과 표준 RDS MySQL(rds_instance).
둘의 메모리 모델이 다르므로 권고 문구는 cluster_meta.engine으로 분기한다
(E-3에서 정정: 이전에는 Aurora 전용 문구를 두 패밀리에 모두 내보냈다).

PG 모듈과 같은 철학이되 MySQL 고유의 메모리 모델을 따른다:

  1. 상호작용 위험(핵심) — MySQL의 진짜 OOM 원인은 단일 파라미터가 아니라
     **per-connection 버퍼(sort/join/read/read_rnd/thread_stack)의 합 ×
     max_connections**가 인스턴스 메모리를 잠식하는 조합이다. Aurora MySQL은
     InnoDB 버퍼 풀을 인스턴스 메모리의 ~75%로 자동 설정하므로, per-thread
     버퍼는 남은 ~25%를 두고 경쟁한다 — 이 합이 메모리의 25%를 넘으면 동시
     부하 시 OOM/스왑 위험.
  2. 엔진 분기: innodb_buffer_pool_size는 Aurora에서는 인스턴스 메모리
     공식으로 자동 관리되므로(PG의 shared_buffers와 동일) 직접 권고하지 않는다.
     표준 RDS MySQL에서는 그 반대로 **직접 튜닝 대상**이며, 실측 사례로
     db.t4g.micro(1GB)에서 128MB가 그대로 남아 있었다.
  3. 확실한 것만 — 메모리 매핑이 안 되거나 표본이 부족하면 침묵한다.

입력은 모두 캐시 DB에 이미 있다(cluster_settings는 mysql_locks가 global
variables를 채우고, metric_snapshots는 cw가, cluster_meta는 meta_collector가).
라이브 클러스터 접근 없이 캐시만 읽는다 — pg_param_fitness와 동일한 패턴.
"""

import json
from datetime import datetime, timezone

from collectors.instance_specs import instance_memory_gb

# 워크로드 대비 임계 — 보수적으로 잡아 오탐을 줄인다(PG 모듈과 정합).
MAXCONN_USAGE_FLOOR = 0.15       # peak가 설정의 15% 미만이면 과다 의심
MAXCONN_MIN_TO_FLAG = 100        # 너무 작은 max_connections는 굳이 안 건드림
CONN_BUFFER_RISK_PCT = 0.25      # per-thread 버퍼 합×max_conn이 메모리 25% 초과 시 경고
CACHE_HIT_FLOOR = 95.0           # 버퍼 캐시 히트율(%) 하한
MIN_SAMPLES = 20                 # 메트릭 표본 최소치

# MySQL global variables 중 per-connection(세션)마다 할당될 수 있는 버퍼.
# Aurora MySQL은 모두 바이트 값으로 보고한다.
_PER_CONN_BUFFERS = [
    "sort_buffer_size", "join_buffer_size", "read_buffer_size",
    "read_rnd_buffer_size", "thread_stack",
]


def _execute(rds_data, cluster_arn, secret_arn, db_name, sql, params=None):
    sql_params = []
    if params:
        for k, v in params.items():
            if isinstance(v, bool):
                sql_params.append({"name": k, "value": {"booleanValue": v}})
            elif isinstance(v, int):
                sql_params.append({"name": k, "value": {"longValue": v}})
            elif isinstance(v, float):
                sql_params.append({"name": k, "value": {"doubleValue": v}})
            else:
                sql_params.append({"name": k, "value": {"stringValue": str(v)}})
    resp = rds_data.execute_statement(
        resourceArn=cluster_arn, secretArn=secret_arn, database=db_name,
        sql=f"/* source=dbops-paramfit */ {sql}", parameters=sql_params,
        includeResultMetadata=True,
    )
    cols = [(c.get("name") or c.get("label") or "") for c in resp.get("columnMetadata", [])]
    out = []
    for rec in resp.get("records", []):
        row = {}
        for i, f in enumerate(rec):
            col = cols[i] if i < len(cols) and cols[i] else f"col_{i}"
            if f.get("isNull"):
                row[col] = None
                continue
            for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                if typ in f:
                    row[col] = f[typ]
                    break
        out.append(row)
    return out


def _to_bytes(value):
    """MySQL global variable(바이트 정수 문자열) → float. 변환 불가 시 None."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_gb(b):
    return f"{b / 1024 ** 3:.1f}GB"


def _fmt_mb(b):
    return f"{b / 1024 ** 2:.0f}MB"


def collect_mysql_param_fitness(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id, snapshot_ts=None):
    """워크로드 기반 MySQL 파라미터 진단을 cluster_health_findings에 적재.

    snapshot_ts: handler가 넘기는 실행 공유 타임스탬프. 같은 ETL 사이클의 다른
    finding과 같은 snapshot_time을 공유해야 대시보드 MAX(snapshot_time) 쿼리에
    함께 잡힌다(없으면 한 배치만 보이는 버그).
    """
    ts = snapshot_ts or datetime.now(timezone.utc).isoformat()

    # --- 1) 메타: 인스턴스 클래스 → 메모리(Sv2는 max ACU로 추정)
    meta_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT instance_class, engine_mode, serverlessv2_max_acu, engine "
        "FROM cluster_meta WHERE cluster_id = :cid", {"cid": cluster_id},
    )
    meta = meta_rows[0] if meta_rows else {}
    instance_class = meta.get("instance_class") or ""
    # E-3: this collector also runs for the rds_instance family (standalone RDS
    # MySQL), where innodb_buffer_pool_size is NOT auto-managed: it is THE
    # parameter to tune. Aurora-specific advice must not be given to it.
    is_aurora = "aurora" in str(meta.get("engine") or "").lower()
    mem_gb, _vcpu = instance_memory_gb(instance_class)
    if mem_gb is None and meta.get("serverlessv2_max_acu"):
        try:
            mem_gb = float(meta["serverlessv2_max_acu"]) * 2.0  # 1 ACU ≈ 2 GB
        except (TypeError, ValueError):
            mem_gb = None
    mem_bytes = mem_gb * 1024 ** 3 if mem_gb else None

    # --- 2) 설정값: 캐시의 cluster_settings (mysql_locks가 채움)
    setting_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT name, value FROM cluster_settings WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    settings = {r["name"]: r["value"] for r in setting_rows}

    findings = []

    def add(check_type, severity, subject, value_str, threshold_str, recommendation, details):
        findings.append({
            "check_type": check_type, "severity": severity, "subject": subject,
            "value_str": value_str, "threshold_str": threshold_str,
            "recommendation": recommendation, "details": json.dumps(details),
        })

    max_conn = None
    try:
        max_conn = int(float(settings["max_connections"])) if settings.get("max_connections") else None
    except (TypeError, ValueError):
        max_conn = None

    # --- 3) 워크로드 메트릭(7일): peak 커넥션, 평균 버퍼 캐시 히트
    conn_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT MAX(value) AS peak, COUNT(*) AS samples FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = 'db_connections' "
        "  AND ts > NOW() - INTERVAL '7 days' "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')", {"cid": cluster_id},
    )
    peak_conn = float(conn_rows[0]["peak"]) if conn_rows and conn_rows[0]["peak"] is not None else None
    conn_samples = int(conn_rows[0]["samples"] or 0) if conn_rows else 0

    # === 규칙 M1: max_connections 과다 할당 ===
    if (max_conn and max_conn >= MAXCONN_MIN_TO_FLAG and peak_conn is not None
            and conn_samples >= MIN_SAMPLES and peak_conn < max_conn * MAXCONN_USAGE_FLOOR):
        usage_pct = peak_conn / max_conn * 100
        add(
            "param_max_connections", "info", "max_connections",
            f"peak {int(peak_conn)} / {max_conn} ({usage_pct:.1f}%)",
            f"7일 peak가 설정의 {MAXCONN_USAGE_FLOOR*100:.0f}% 미만",
            f"최근 7일 동시 연결 peak가 {int(peak_conn)}인데 max_connections는 "
            f"{max_conn}으로 설정돼 있습니다(사용률 {usage_pct:.1f}%). MySQL은 각 연결마다 "
            f"sort/join/read 버퍼를 예약할 수 있어, 과다 할당은 메모리 상한을 끌어올립니다. "
            f"기본 파라미터 그룹의 인스턴스 메모리 공식이 만든 값이라면 파라미터 조정보다 "
            f"인스턴스 다운사이즈가 더 효과적일 수 있습니다.",
            {"current": max_conn, "peak_7d": int(peak_conn), "usage_pct": round(usage_pct, 1)},
        )

    # === 규칙 M2: per-connection 버퍼 × max_connections 메모리 위험 (상호작용) ===
    per_conn_bytes = 0.0
    have_buffers = False
    buffer_detail = {}
    for name in _PER_CONN_BUFFERS:
        b = _to_bytes(settings.get(name))
        if b is not None:
            per_conn_bytes += b
            have_buffers = True
            buffer_detail[name] = int(b)
    if have_buffers and max_conn and mem_bytes:
        worst = per_conn_bytes * max_conn
        worst_pct = worst / mem_bytes * 100
        if worst > mem_bytes * CONN_BUFFER_RISK_PCT:
            add(
                "param_mysql_conn_buffers", "warning", "per-connection 버퍼 × max_connections",
                f"최악 {_fmt_gb(worst)} / 인스턴스 {mem_gb:.0f}GB ({worst_pct:.0f}%)",
                f"인스턴스 메모리의 {CONN_BUFFER_RISK_PCT*100:.0f}% 초과",
                f"연결당 세션 버퍼 합({_fmt_mb(per_conn_bytes)} = "
                f"sort+join+read+read_rnd+thread_stack) × max_connections {max_conn} = "
                f"최악의 경우 {_fmt_gb(worst)}(인스턴스 {mem_gb:.0f}GB의 {worst_pct:.0f}%)까지 "
                f"점유할 수 있습니다. "
                + (
                    "Aurora MySQL은 InnoDB 버퍼 풀을 메모리의 ~75%로 자동 확보하므로 "
                    "per-thread 버퍼는 남은 여유를 두고 경쟁합니다"
                    if is_aurora else
                    f"이 인스턴스에서는 innodb_buffer_pool_size"
                    f"({_fmt_mb(_to_bytes(settings.get('innodb_buffer_pool_size')) or 0)})가 "
                    f"직접 설정값이므로, 버퍼 풀과 per-connection 버퍼의 합이 인스턴스 메모리를 "
                    f"넘지 않는지 함께 확인해야 합니다"
                )
                + ". 복잡한 쿼리가 동시에 몰리면 OOM/스왑 위험이 있습니다. 전역 버퍼를 낮추고 "
                "메모리 집약 쿼리에만 세션 단위(SET SESSION sort_buffer_size=…)로 올리거나, "
                "max_connections를 실측 peak에 맞춰 줄이는 방안을 검토하세요.",
                {"per_conn_bytes": int(per_conn_bytes), "max_connections": max_conn,
                 "instance_memory_gb": mem_gb, "worst_case_pct": round(worst_pct, 1),
                 "buffers": buffer_detail},
            )

    # === 규칙 M3: 버퍼 캐시 히트율 저조 (인스턴스 메모리 신호로) ===
    # 두 metric_type을 한 쿼리로 재는 이유(E-3에서 실측한 버그): 이 규칙은
    # 'buffer_cache_hit'만 읽었는데, 그 metric_type을 쓰는 수집기는 Aurora CW
    # (cw_collector.BufferCacheHitRatio)와 DocumentDB뿐이다. rds_instance
    # 패밀리(RDS MySQL)는 innodb_buffer_pool_hit_rate만 쓰므로 이 규칙은 그
    # 패밀리에서 영구히 죽어 있었다. Aurora 경로의 값은 바꾸지 않기 위해
    # 평균을 섞지 않고 CW를 우선하고, 표본이 없을 때만 InnoDB로 폴백한다
    # (둘은 정의가 다른 측정치라 하나의 AVG로 합치면 안 된다).
    hit_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT "
        "  AVG(CASE WHEN metric_type = 'buffer_cache_hit' THEN value END) AS cw_hit, "
        "  COUNT(CASE WHEN metric_type = 'buffer_cache_hit' THEN 1 END) AS cw_samples, "
        "  AVG(CASE WHEN metric_type = 'innodb_buffer_pool_hit_rate' THEN value END) AS innodb_hit, "
        "  COUNT(CASE WHEN metric_type = 'innodb_buffer_pool_hit_rate' THEN 1 END) AS innodb_samples "
        "FROM metric_snapshots "
        "WHERE cluster_id = :cid "
        "  AND metric_type IN ('buffer_cache_hit', 'innodb_buffer_pool_hit_rate') "
        "  AND ts > NOW() - INTERVAL '7 days' "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')", {"cid": cluster_id},
    )
    hr = hit_rows[0] if hit_rows else {}

    def _pair(avg_key, cnt_key):
        v = hr.get(avg_key)
        return (float(v) if v is not None else None), int(hr.get(cnt_key) or 0)

    cw_hit, cw_samples = _pair("cw_hit", "cw_samples")
    innodb_hit, innodb_samples = _pair("innodb_hit", "innodb_samples")
    if cw_hit is not None and cw_samples >= MIN_SAMPLES:
        avg_hit, hit_samples, hit_source = cw_hit, cw_samples, "buffer_cache_hit"
    else:
        avg_hit, hit_samples, hit_source = innodb_hit, innodb_samples, "innodb_buffer_pool_hit_rate"
    if avg_hit is not None and hit_samples >= MIN_SAMPLES and avg_hit < CACHE_HIT_FLOOR:
        add(
            "param_buffer_cache_hit", "info", "buffer cache hit ratio",
            f"{avg_hit:.1f}% (7일 평균)",
            f"{CACHE_HIT_FLOOR:.0f}% 미만",
            f"버퍼 캐시 히트율이 7일 평균 {avg_hit:.1f}%로 {CACHE_HIT_FLOOR:.0f}% 미만입니다 — "
            f"작업셋이 인스턴스 메모리(InnoDB 버퍼 풀)를 초과해 디스크 I/O가 늘고 있을 수 "
            f"있습니다. "
            + (
                "Aurora MySQL은 InnoDB 버퍼 풀을 인스턴스 메모리 비율로 크게 자동 설정하므로, "
                "파라미터 조정보다 인스턴스 메모리 상향(또는 Serverless v2 max ACU 상향)이 "
                "더 효과적인 경우가 많습니다. Cost 탭의 라이트사이징 권고와 함께 검토하세요."
                if is_aurora else
                f"이 인스턴스는 innodb_buffer_pool_size"
                f"({_fmt_mb(_to_bytes(settings.get('innodb_buffer_pool_size')) or 0)})가 "
                f"직접 설정값입니다(Aurora처럼 자동으로 크게 잡히지 않습니다). 인스턴스 메모리에 "
                f"여유가 있으면 파라미터 그룹에서 innodb_buffer_pool_size를 올리는 것이 1차 조치이고, "
                f"이미 메모리 대비 크게 잡혀 있다면 인스턴스 메모리 상향을 검토하세요. "
                f"MySQL 8.x에서 이 파라미터는 dynamic이라 재시작 없이 적용되지만, 온라인 리사이즈는 "
                f"청크 단위로 진행되며 진행 중에는 버퍼 풀 경합이 있습니다."
            ),
            {"avg_hit_pct": round(avg_hit, 1), "samples": hit_samples,
             "metric_type": hit_source, "instance_class": instance_class,
             "innodb_buffer_pool_size": settings.get("innodb_buffer_pool_size")},
        )

    # --- 단일 snapshot_time으로 일괄 적재
    for f in findings:
        _execute(
            rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
            "INSERT INTO cluster_health_findings "
            "(cluster_id, snapshot_time, check_type, severity, subject, value_str, threshold_str, recommendation, details) "
            "VALUES (:cluster_id, :ts::timestamptz, :check_type, :severity, :subject, :value_str, :threshold_str, :recommendation, :details::jsonb)",
            {"cluster_id": cluster_id, "ts": ts, "check_type": f["check_type"],
             "severity": f["severity"], "subject": f["subject"], "value_str": f["value_str"],
             "threshold_str": f["threshold_str"], "recommendation": f["recommendation"],
             "details": f["details"]},
        )

    return {
        "cluster_id": cluster_id,
        "instance_class": instance_class,
        "instance_memory_gb": mem_gb,
        "max_connections": max_conn,
        "peak_connections_7d": int(peak_conn) if peak_conn is not None else None,
        "findings_emitted": len(findings),
        "memory_mapping": "ok" if mem_gb else "unmapped (메모리 의존 규칙 skip)",
    }
