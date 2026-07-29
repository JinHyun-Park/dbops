"""Parameter Fitness 진단 단위 테스트.

collect_param_fitness는 RDS Data API에 의존하므로, 여기서는 순수 헬퍼
(설정 단위 변환·인스턴스 메모리 매핑)와 진단 규칙의 경계 조건을 검증한다.
규칙 자체는 _execute를 모킹해 캐시 응답을 주입하고 emit된 finding을 확인한다.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"


def _load(mod_name, rel):
    # collectors 패키지 상대 임포트(from collectors.instance_specs ...) 해결을 위해
    # etl_collector를 sys.path에 올린다.
    import sys
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


specs = _load("instance_specs", "collectors/instance_specs.py")
pf = _load("pg_param_fitness", "collectors/pg_param_fitness.py")


# ---- 인스턴스 메모리 매핑 ----
def test_instance_memory_mapping():
    assert specs.instance_memory_gb("db.r6g.large") == (16, 2)
    assert specs.instance_memory_gb("db.r6g.2xlarge") == (64, 8)
    assert specs.instance_memory_gb("db.t3.medium") == (4, 2)


def test_instance_memory_unmapped_returns_none():
    # 미지원 클래스·serverless·빈 값 → (None, None)로 메모리 의존 규칙 skip
    assert specs.instance_memory_gb("db.serverless") == (None, None)
    assert specs.instance_memory_gb("db.future.99xlarge") == (None, None)
    assert specs.instance_memory_gb("") == (None, None)


# ---- pg_settings 단위 → 바이트 ----
def test_setting_bytes_units():
    assert pf._setting_bytes("2048", "8kB") == 2048 * 8 * 1024  # shared_buffers 블록
    assert pf._setting_bytes("4096", "kB") == 4096 * 1024       # work_mem kB
    assert pf._setting_bytes("1716", "") == 1716                # max_connections (단위 없음)
    assert pf._setting_bytes("nope", "kB") is None              # 파싱 실패


# ---- 진단 규칙: work_mem × max_connections 상호작용 위험 ----
def _mock_execute(meta, settings, metrics, dead_tables):
    """_execute 호출을 SQL 키워드로 분기해 가짜 캐시 응답 반환."""
    def fake(rds, arn, secret, db, sql, params=None):
        if "FROM cluster_meta" in sql:
            return [meta]
        if "FROM cluster_settings" in sql:
            return [{"name": n, "value": v, "unit": u} for n, (v, u) in settings.items()]
        if "metric_type = 'db_connections'" in sql:
            return [{"peak": metrics.get("peak_conn"), "samples": metrics.get("conn_samples", 0)}]
        if "metric_type = 'buffer_cache_hit'" in sql:
            return [{"avg_hit": metrics.get("avg_hit"), "samples": metrics.get("hit_samples", 0)}]
        if "FROM table_stats" in sql:
            return [{"n": dead_tables}]
        if sql.strip().upper().startswith("INSERT"):
            return []
        return []
    return fake


def test_work_mem_interaction_risk_flagged():
    # r6g.large(16GB), work_mem 256MB × max_connections 200 = 50GB worst → 위험
    settings = {
        "max_connections": ("200", ""),
        "work_mem": ("262144", "kB"),  # 256MB
        "effective_cache_size": ("12582912", "8kB"),  # 96GB → 낮음 아님
    }
    meta = {"instance_class": "db.r6g.large", "engine_mode": "provisioned", "serverlessv2_max_acu": None}
    metrics = {"peak_conn": 50, "conn_samples": 100, "avg_hit": 99.9, "hit_samples": 100}
    emitted = []
    with patch.object(pf, "_execute", side_effect=_mock_execute(meta, settings, metrics, 0)) as m:
        # INSERT 호출을 가로채 finding check_type 수집
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                emitted.append(params["check_type"])
            return _mock_execute(meta, settings, metrics, 0)(rds, arn, secret, db, sql, params)
        m.side_effect = capture
        result = pf.collect_param_fitness(MagicMock(), "arn", "secret", "db", "c1", snapshot_ts="2026-06-11T00:00:00Z")
    assert "param_work_mem_risk" in emitted
    assert result["instance_memory_gb"] == 16


def test_no_findings_when_settings_healthy():
    # 적정 설정 + 정상 워크로드 → finding 없음(= 건강)
    settings = {
        "max_connections": ("100", ""),
        "work_mem": ("4096", "kB"),  # 4MB → 4MB×100=400MB, 16GB의 2.4%
        "effective_cache_size": ("1572864", "8kB"),  # 12GB = 16GB의 75%
        "autovacuum_max_workers": ("3", ""),
    }
    meta = {"instance_class": "db.r6g.large", "engine_mode": "provisioned", "serverlessv2_max_acu": None}
    metrics = {"peak_conn": 40, "conn_samples": 100, "avg_hit": 99.5, "hit_samples": 100}
    emitted = []
    with patch.object(pf, "_execute") as m:
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                emitted.append(params["check_type"])
            return _mock_execute(meta, settings, metrics, 0)(rds, arn, secret, db, sql, params)
        m.side_effect = capture
        pf.collect_param_fitness(MagicMock(), "arn", "secret", "db", "c1", snapshot_ts="2026-06-11T00:00:00Z")
    assert emitted == []


def test_unmapped_instance_skips_memory_rules():
    # 메모리 매핑 불가(serverless 정보도 없음) → work_mem/ecs/cache 규칙 skip,
    # 메모리 무관 규칙(max_connections 과다)만 가능
    settings = {
        "max_connections": ("500", ""),
        "work_mem": ("262144", "kB"),  # 메모리 모르면 위험 계산 불가 → skip
    }
    meta = {"instance_class": "db.unknownfamily.bigxl", "engine_mode": "provisioned", "serverlessv2_max_acu": None}
    metrics = {"peak_conn": 10, "conn_samples": 100, "avg_hit": 99.9, "hit_samples": 100}
    emitted = []
    with patch.object(pf, "_execute") as m:
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                emitted.append(params["check_type"])
            return _mock_execute(meta, settings, metrics, 0)(rds, arn, secret, db, sql, params)
        m.side_effect = capture
        result = pf.collect_param_fitness(MagicMock(), "arn", "secret", "db", "c1", snapshot_ts="2026-06-11T00:00:00Z")
    assert "param_work_mem_risk" not in emitted   # 메모리 미상이라 skip
    assert "param_max_connections" in emitted      # peak 10 / 500 = 2% → 과다
    assert result["memory_mapping"].startswith("unmapped")


def test_the_window_wording_is_derived_from_the_window_that_was_measured():
    """The PostgreSQL twin of the mysql_param_fitness finding: the peak-connection
    and cache-hit statements said INTERVAL '7 days' while their findings said "7일"
    as separate hardcoded literals, so widening the window left the finding
    claiming a 7-day average of a longer measurement with nothing failing. Both
    halves now come from WINDOW_DAYS; this drives the constant to a different
    value and requires the SQL and the wording to move together."""
    settings = {
        "max_connections": ("500", ""),
        "work_mem": ("4096", "kB"),
        "effective_cache_size": ("1572864", "8kB"),
    }
    meta = {"instance_class": "db.r6g.large", "engine_mode": "provisioned",
            "serverlessv2_max_acu": None}
    # peak 10 / 500 = 2% -> M1 fires; avg_hit 80% < 95% floor -> M5 fires.
    metrics = {"peak_conn": 10, "conn_samples": 100, "avg_hit": 80.0, "hit_samples": 100}
    rows, sqls = [], []
    with patch.object(pf, "WINDOW_DAYS", 30), patch.object(pf, "_execute") as m:
        def capture(rds, arn, secret, db, sql, params=None):
            sqls.append(sql)
            if sql.strip().upper().startswith("INSERT"):
                rows.append(params)
            return _mock_execute(meta, settings, metrics, 0)(rds, arn, secret, db, sql, params)
        m.side_effect = capture
        pf.collect_param_fitness(MagicMock(), "arn", "secret", "db", "c1",
                                 snapshot_ts="2026-06-11T00:00:00Z")

    windowed = [s for s in sqls if "metric_snapshots" in s]
    assert len(windowed) == 2, "peak-connections and cache-hit windows"
    for sql in windowed:
        assert "INTERVAL '30 days'" in sql, sql
        assert "'7 days'" not in sql, sql

    (hit,) = [r for r in rows if r["check_type"] == "param_buffer_cache_hit"]
    assert hit["value_str"] == "80.0% (30일 평균)"
    assert "30일 평균" in hit["recommendation"]
    (conn,) = [r for r in rows if r["check_type"] == "param_max_connections"]
    assert "30일 peak" in conn["threshold_str"]
    assert "최근 30일" in conn["recommendation"]
    for row in (hit, conn):
        assert "7일" not in row["value_str"] + row["threshold_str"] + row["recommendation"]
