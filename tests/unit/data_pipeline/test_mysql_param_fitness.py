"""MySQL Parameter Fitness 진단 단위 테스트.

collect_mysql_param_fitness는 RDS Data API에 의존하므로 _execute를 모킹해
캐시 응답(메타·MySQL global variables·메트릭)을 주입하고 emit된 finding을
확인한다. 핵심은 per-connection 버퍼 × max_connections OOM 상호작용 규칙.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"


def _load(mod_name, rel):
    import sys
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_load("instance_specs", "collectors/instance_specs.py")
pf = _load("mysql_param_fitness", "collectors/mysql_param_fitness.py")


def _mock_execute(meta, settings, metrics):
    """_execute 호출을 SQL 키워드로 분기. settings는 {name: value} (MySQL은 바이트)."""
    def fake(rds, arn, secret, db, sql, params=None):
        if "FROM cluster_meta" in sql:
            return [meta]
        if "FROM cluster_settings" in sql:
            return [{"name": n, "value": v} for n, v in settings.items()]
        if "metric_type = 'db_connections'" in sql:
            return [{"peak": metrics.get("peak_conn"), "samples": metrics.get("conn_samples", 0)}]
        if "metric_type = 'buffer_cache_hit'" in sql:
            return [{"avg_hit": metrics.get("avg_hit"), "samples": metrics.get("hit_samples", 0)}]
        if sql.strip().upper().startswith("INSERT"):
            return []
        return []
    return fake


def _run(meta, settings, metrics):
    emitted = []
    with patch.object(pf, "_execute") as m:
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                emitted.append(params["check_type"])
            return _mock_execute(meta, settings, metrics)(rds, arn, secret, db, sql, params)
        m.side_effect = capture
        result = pf.collect_mysql_param_fitness(
            MagicMock(), "arn", "secret", "db", "c1", snapshot_ts="2026-06-11T00:00:00Z"
        )
    return emitted, result


_MB = 1024 ** 2


def test_conn_buffer_interaction_risk_flagged():
    # r6g.large(16GB). per-conn 버퍼 합 13MB × max_connections 1000 = 13GB worst
    # → 16GB의 25%(4GB) 초과 → 경고.
    settings = {
        "max_connections": "1000",
        "sort_buffer_size": str(4 * _MB),
        "join_buffer_size": str(4 * _MB),
        "read_buffer_size": str(2 * _MB),
        "read_rnd_buffer_size": str(2 * _MB),
        "thread_stack": str(1 * _MB),
    }
    meta = {"instance_class": "db.r6g.large", "engine_mode": "provisioned", "serverlessv2_max_acu": None}
    # peak 500/1000 = 50% → M1 안 뜸. avg_hit 99 → M3 안 뜸.
    metrics = {"peak_conn": 500, "conn_samples": 100, "avg_hit": 99.5, "hit_samples": 100}
    emitted, result = _run(meta, settings, metrics)
    assert "param_mysql_conn_buffers" in emitted
    assert "param_max_connections" not in emitted
    assert result["instance_memory_gb"] == 16


def test_no_findings_when_settings_healthy():
    # 기본값에 가까운 작은 버퍼 + 정상 워크로드 → finding 없음.
    settings = {
        "max_connections": "200",
        "sort_buffer_size": str(256 * 1024),
        "join_buffer_size": str(256 * 1024),
        "read_buffer_size": str(128 * 1024),
        "read_rnd_buffer_size": str(256 * 1024),
        "thread_stack": str(256 * 1024),
    }
    meta = {"instance_class": "db.r6g.large", "engine_mode": "provisioned", "serverlessv2_max_acu": None}
    metrics = {"peak_conn": 80, "conn_samples": 100, "avg_hit": 99.5, "hit_samples": 100}
    emitted, _ = _run(meta, settings, metrics)
    assert emitted == []


def test_max_connections_over_allocation_flagged():
    # peak 10 / max 500 = 2% → 과다 할당(메모리 무관 규칙).
    settings = {"max_connections": "500", "sort_buffer_size": str(256 * 1024)}
    meta = {"instance_class": "db.r6g.large", "engine_mode": "provisioned", "serverlessv2_max_acu": None}
    metrics = {"peak_conn": 10, "conn_samples": 100, "avg_hit": 99.9, "hit_samples": 100}
    emitted, _ = _run(meta, settings, metrics)
    assert "param_max_connections" in emitted


def test_unmapped_instance_skips_memory_rule():
    # 메모리 매핑 불가 → conn_buffers(메모리 의존) skip, max_connections만 가능.
    settings = {
        "max_connections": "500",
        "sort_buffer_size": str(8 * _MB),
        "join_buffer_size": str(8 * _MB),
    }
    meta = {"instance_class": "db.unknownfamily.bigxl", "engine_mode": "provisioned", "serverlessv2_max_acu": None}
    metrics = {"peak_conn": 10, "conn_samples": 100, "avg_hit": 99.9, "hit_samples": 100}
    emitted, result = _run(meta, settings, metrics)
    assert "param_mysql_conn_buffers" not in emitted  # 메모리 미상 → skip
    assert "param_max_connections" in emitted          # peak 2% → 과다
    assert result["memory_mapping"].startswith("unmapped")


def test_serverless_v2_memory_from_max_acu():
    # Sv2(db.serverless)는 instance_specs가 None → max ACU로 메모리 추정(8 ACU≈16GB).
    settings = {
        "max_connections": "1000",
        "sort_buffer_size": str(4 * _MB),
        "join_buffer_size": str(4 * _MB),
        "read_buffer_size": str(2 * _MB),
        "read_rnd_buffer_size": str(2 * _MB),
        "thread_stack": str(1 * _MB),
    }
    meta = {"instance_class": "db.serverless", "engine_mode": "serverless", "serverlessv2_max_acu": 8.0}
    metrics = {"peak_conn": 500, "conn_samples": 100, "avg_hit": 99.5, "hit_samples": 100}
    emitted, result = _run(meta, settings, metrics)
    assert result["instance_memory_gb"] == 16.0  # 8 ACU × 2
    assert "param_mysql_conn_buffers" in emitted  # 13GB > 16GB의 25%
