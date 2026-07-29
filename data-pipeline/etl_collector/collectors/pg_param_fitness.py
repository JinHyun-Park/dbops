"""Parameter Fitness — 이 클러스터의 실제 워크로드 기준 파라미터 적정성 진단.

기존 setting_misconfigured 점검(pg_health_checks)은 "log_connections는 on이
좋다" 같은 워크로드-무관 정적 베스트프랙티스다. 이 모듈은 정반대로,
**이 인스턴스의 실측 데이터**(peak 커넥션·버퍼 캐시 히트·dead tuple 압력·
인스턴스 메모리)에 비춰 현재 설정값이 과/소한지를 근거와 함께 판단한다.

핵심 차별점:
  1. 상호작용 위험 — 단일 파라미터가 아니라 work_mem × max_connections가
     인스턴스 메모리를 초과할 수 있는 조합을 잡는다(실제 OOM의 흔한 원인,
     상용 도구도 잘 못 짚는 부분).
  2. Aurora 특수성 — Aurora PG는 shared_buffers·max_connections를 인스턴스
     메모리 공식으로 자동 설정하고 일부는 변경이 무의미하다. vanilla PG
     베스트프랙티스를 그대로 들이대지 않는다.
  3. 확실한 것만 — 메모리 매핑이 안 되거나 표본이 부족하면 침묵한다. 틀린
     권고로 신뢰를 깨느니 안 내는 쪽.

모든 입력 데이터는 캐시 DB에 이미 있다(cluster_settings는 pg_locks가,
metric_snapshots는 cw/pi가, table_stats는 pg_table_stats가, cluster_meta는
meta_collector가 채운다). 라이브 클러스터 접근 없이 캐시만 읽는다 —
cost_check와 동일한 패턴.
"""

import json
import re
from datetime import datetime, timezone

from collectors.instance_specs import instance_memory_gb

# 워크로드 대비 과다 판정 임계 — 보수적으로 잡아 오탐을 줄인다.
MAXCONN_USAGE_FLOOR = 0.15      # peak가 설정의 15% 미만이면 과다 의심
MAXCONN_MIN_TO_FLAG = 100       # 너무 작은 max_connections는 굳이 안 건드림
WORKMEM_RISK_PCT = 0.25         # work_mem×max_conn이 메모리의 25% 초과 시 경고
ECS_LOW_PCT = 0.5               # effective_cache_size가 메모리의 50% 미만이면 낮음
ECS_TARGET_PCT = 0.75           # 권장 비율(설명용)
CACHE_HIT_FLOOR = 95.0          # 버퍼 캐시 히트율(%) 하한
DEAD_RATIO_PCT = 20.0           # dead tuple 비율 임계
DEAD_TABLES_TO_FLAG = 3         # 이 개수 이상 테이블이 압력이면 worker 진단
MIN_SAMPLES = 20                # 메트릭 표본 최소치
# 워크로드 메트릭(peak 커넥션·버퍼 캐시 히트)을 재는 윈도. SQL의 INTERVAL과
# finding 문구가 이 상수 하나에서 나온다. 예전에는 statement가 INTERVAL '7 days'
# 이고 문구가 "7일 평균"으로 각각 하드코딩돼 있어서, 윈도를 넓히면 finding이
# 30일치 측정을 "7일 평균"이라고 말하는데도 깨지는 것이 아무것도 없었다.
WINDOW_DAYS = 7


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


def _setting_bytes(value, unit):
    """pg_settings 값(숫자 문자열) + unit('8kB','kB','MB',''…)을 바이트로.
    변환 불가 시 None."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    u = (unit or "").strip()
    if u in ("", "B"):
        return n
    m = re.match(r"(\d*)\s*([kKmMgG]B)$", u)
    if not m:
        return None
    mult = int(m.group(1)) if m.group(1) else 1
    base = {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}[m.group(2).upper()]
    return n * mult * base


def _fmt_gb(b):
    return f"{b / 1024 ** 3:.1f}GB"


def _fmt_mb(b):
    return f"{b / 1024 ** 2:.0f}MB"


def collect_param_fitness(rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name, cluster_id, snapshot_ts=None):
    """워크로드 기반 파라미터 진단을 실행해 cluster_health_findings에 적재.

    snapshot_ts: handler가 넘기는 실행 공유 타임스탬프. 같은 ETL 사이클의
    다른 finding(vacuum/cost 등)과 같은 snapshot_time을 공유해야 대시보드의
    MAX(snapshot_time) 쿼리에 함께 잡힌다(없으면 한 배치만 보이는 버그).
    """
    ts = snapshot_ts or datetime.now(timezone.utc).isoformat()

    # --- 1) 메타: 인스턴스 클래스 → 메모리, RDS 보고 max_connections
    meta_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT instance_class, engine_mode, serverlessv2_max_acu "
        "FROM cluster_meta WHERE cluster_id = :cid", {"cid": cluster_id},
    )
    meta = meta_rows[0] if meta_rows else {}
    instance_class = meta.get("instance_class") or ""
    mem_gb, _vcpu = instance_memory_gb(instance_class)
    # Serverless v2는 max ACU로 메모리 추정(1 ACU ≈ 2 GB).
    if mem_gb is None and meta.get("serverlessv2_max_acu"):
        try:
            mem_gb = float(meta["serverlessv2_max_acu"]) * 2.0
        except (TypeError, ValueError):
            mem_gb = None
    mem_bytes = mem_gb * 1024 ** 3 if mem_gb else None

    # --- 2) 설정값: 캐시의 cluster_settings (pg_locks가 채움)
    setting_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT name, value, unit FROM cluster_settings WHERE cluster_id = :cid",
        {"cid": cluster_id},
    )
    settings = {r["name"]: (r["value"], r["unit"]) for r in setting_rows}

    def sval(name):
        return settings.get(name, (None, None))[0]

    def sbytes(name):
        v, u = settings.get(name, (None, None))
        return _setting_bytes(v, u)

    findings = []

    def add(check_type, severity, subject, value_str, threshold_str, recommendation, details):
        findings.append({
            "check_type": check_type, "severity": severity, "subject": subject,
            "value_str": value_str, "threshold_str": threshold_str,
            "recommendation": recommendation, "details": json.dumps(details),
        })

    max_conn = None
    try:
        max_conn = int(float(sval("max_connections"))) if sval("max_connections") else None
    except (TypeError, ValueError):
        max_conn = None

    # --- 3) 워크로드 메트릭(WINDOW_DAYS일): peak 커넥션, 평균 버퍼 캐시 히트
    conn_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT MAX(value) AS peak, COUNT(*) AS samples FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = 'db_connections' "
        f"  AND ts > NOW() - INTERVAL '{WINDOW_DAYS} days' "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')", {"cid": cluster_id},
    )
    peak_conn = float(conn_rows[0]["peak"]) if conn_rows and conn_rows[0]["peak"] is not None else None
    conn_samples = int(conn_rows[0]["samples"] or 0) if conn_rows else 0

    # === 규칙 1: max_connections 과다 할당 ===
    if (max_conn and max_conn >= MAXCONN_MIN_TO_FLAG and peak_conn is not None
            and conn_samples >= MIN_SAMPLES and peak_conn < max_conn * MAXCONN_USAGE_FLOOR):
        usage_pct = peak_conn / max_conn * 100
        add(
            "param_max_connections", "info", "max_connections",
            f"peak {int(peak_conn)} / {max_conn} ({usage_pct:.1f}%)",
            f"{WINDOW_DAYS}일 peak가 설정의 {MAXCONN_USAGE_FLOOR*100:.0f}% 미만",
            f"최근 {WINDOW_DAYS}일 동시 연결 peak가 {int(peak_conn)}인데 max_connections는 "
            f"{max_conn}으로 설정돼 있습니다(사용률 {usage_pct:.1f}%). 각 연결 슬롯은 "
            f"work_mem 등 백엔드 메모리를 예약하므로, 과다 할당은 메모리를 선점합니다. "
            f"Aurora가 인스턴스 메모리 공식으로 자동 설정한 값이라면 파라미터 조정보다 "
            f"인스턴스 다운사이즈가 더 효과적일 수 있습니다.",
            {"current": max_conn, "peak_7d": int(peak_conn), "usage_pct": round(usage_pct, 1)},
        )

    # === 규칙 2: work_mem × max_connections 메모리 위험 (상호작용) ===
    wm_bytes = sbytes("work_mem")
    if wm_bytes and max_conn and mem_bytes:
        worst = wm_bytes * max_conn
        worst_pct = worst / mem_bytes * 100
        if worst > mem_bytes * WORKMEM_RISK_PCT:
            add(
                "param_work_mem_risk", "warning", "work_mem × max_connections",
                f"최악 {_fmt_gb(worst)} / 인스턴스 {mem_gb:.0f}GB ({worst_pct:.0f}%)",
                f"인스턴스 메모리의 {WORKMEM_RISK_PCT*100:.0f}% 초과",
                f"work_mem {_fmt_mb(wm_bytes)} × max_connections {max_conn} = 최악의 경우 "
                f"정렬/해시 작업 메모리가 {_fmt_gb(worst)}(인스턴스 {mem_gb:.0f}GB의 "
                f"{worst_pct:.0f}%)까지 점유할 수 있습니다. 복잡한 쿼리가 동시에 몰리면 "
                f"OOM 위험이 있습니다. work_mem를 낮추거나 max_connections를 줄이거나, "
                f"메모리 집약 쿼리에만 세션 단위로 work_mem를 올리는 방식을 검토하세요.",
                {"work_mem_mb": round(wm_bytes / 1024 ** 2, 1), "max_connections": max_conn,
                 "instance_memory_gb": mem_gb, "worst_case_pct": round(worst_pct, 1)},
            )

    # === 규칙 3: effective_cache_size가 인스턴스 메모리 대비 낮음 ===
    ecs_bytes = sbytes("effective_cache_size")
    if ecs_bytes and mem_bytes and ecs_bytes < mem_bytes * ECS_LOW_PCT:
        cur_pct = ecs_bytes / mem_bytes * 100
        target = mem_bytes * ECS_TARGET_PCT
        add(
            "param_effective_cache", "info", "effective_cache_size",
            f"{_fmt_gb(ecs_bytes)} / 인스턴스 {mem_gb:.0f}GB ({cur_pct:.0f}%)",
            f"메모리의 {ECS_LOW_PCT*100:.0f}% 미만",
            f"effective_cache_size가 인스턴스 메모리({mem_gb:.0f}GB)의 {cur_pct:.0f}%로 "
            f"설정돼 있습니다. 이 값이 낮으면 플래너가 OS/공유 캐시 효과를 과소평가해 "
            f"인덱스 스캔보다 seq scan을 선호할 수 있습니다. 일반적으로 메모리의 "
            f"~{ECS_TARGET_PCT*100:.0f}%(약 {_fmt_gb(target)}) 권장 — 실제 메모리를 더 "
            f"쓰는 게 아니라 플래너 힌트일 뿐이라 안전합니다.",
            {"current_gb": round(ecs_bytes / 1024 ** 3, 1), "instance_memory_gb": mem_gb,
             "current_pct": round(cur_pct, 1), "suggested_gb": round(target / 1024 ** 3, 1)},
        )

    # === 규칙 4: autovacuum worker가 dead-tuple 압력 대비 부족 ===
    aw_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT COUNT(*) AS n FROM ("
        "  SELECT schema_name, table_name, "
        "    MAX(n_dead_tup) AS dead, MAX(n_live_tup) AS live "
        "  FROM table_stats WHERE cluster_id = :cid "
        "    AND snapshot_time > NOW() - INTERVAL '1 day' "
        "  GROUP BY schema_name, table_name"
        ") t WHERE (dead + live) > 1000 AND dead::float / NULLIF(dead + live, 0) * 100 > :ratio",
        {"cid": cluster_id, "ratio": DEAD_RATIO_PCT},
    )
    dead_tables = int(aw_rows[0]["n"] or 0) if aw_rows else 0
    try:
        aw = int(float(sval("autovacuum_max_workers"))) if sval("autovacuum_max_workers") else None
    except (TypeError, ValueError):
        aw = None
    if dead_tables >= DEAD_TABLES_TO_FLAG and aw is not None and aw <= 3:
        add(
            "param_autovacuum_workers", "info", "autovacuum_max_workers",
            f"{aw} worker / dead>{DEAD_RATIO_PCT:.0f}% 테이블 {dead_tables}개",
            f"압력 테이블 {DEAD_TABLES_TO_FLAG}개 이상 + worker ≤ 3",
            f"dead tuple 비율이 {DEAD_RATIO_PCT:.0f}%를 넘는 테이블이 {dead_tables}개인데 "
            f"autovacuum_max_workers는 {aw}개입니다. autovacuum이 정리를 못 따라가면 "
            f"bloat가 누적됩니다. worker 수 상향 또는 압력이 큰 테이블에 per-table "
            f"autovacuum_vacuum_scale_factor를 낮춰 더 자주 돌게 하는 방안을 검토하세요.",
            {"autovacuum_max_workers": aw, "tables_under_pressure": dead_tables,
             "dead_ratio_threshold_pct": DEAD_RATIO_PCT},
        )

    # === 규칙 5: 버퍼 캐시 히트율 저조 (Aurora는 인스턴스 메모리 신호로) ===
    hit_rows = _execute(
        rds_data, cache_cluster_arn, cache_secret_arn, cache_db_name,
        "SELECT AVG(value) AS avg_hit, COUNT(*) AS samples FROM metric_snapshots "
        "WHERE cluster_id = :cid AND metric_type = 'buffer_cache_hit' "
        f"  AND ts > NOW() - INTERVAL '{WINDOW_DAYS} days' "
        "  AND (dimensions IS NULL OR dimensions::text = '{}')", {"cid": cluster_id},
    )
    avg_hit = float(hit_rows[0]["avg_hit"]) if hit_rows and hit_rows[0]["avg_hit"] is not None else None
    hit_samples = int(hit_rows[0]["samples"] or 0) if hit_rows else 0
    if avg_hit is not None and hit_samples >= MIN_SAMPLES and avg_hit < CACHE_HIT_FLOOR:
        add(
            "param_buffer_cache_hit", "info", "buffer cache hit ratio",
            f"{avg_hit:.1f}% ({WINDOW_DAYS}일 평균)",
            f"{CACHE_HIT_FLOOR:.0f}% 미만",
            f"버퍼 캐시 히트율이 {WINDOW_DAYS}일 평균 {avg_hit:.1f}%로 "
            f"{CACHE_HIT_FLOOR:.0f}% 미만입니다 — "
            f"작업셋이 인스턴스 메모리를 초과해 디스크 I/O가 늘고 있을 수 있습니다. "
            f"Aurora PG는 shared_buffers를 인스턴스 메모리 비율로 크게 자동 설정하므로, "
            f"파라미터 조정보다 인스턴스 메모리 상향(또는 Serverless v2 max ACU 상향)이 "
            f"더 효과적인 경우가 많습니다. Cost 탭의 라이트사이징 권고와 함께 검토하세요.",
            {"avg_hit_pct": round(avg_hit, 1), "samples": hit_samples,
             "instance_class": instance_class},
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
