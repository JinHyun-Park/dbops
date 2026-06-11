"""고갈 예측 경보 collector 테스트.

_execute를 모킹해 캐시 응답(메타·메트릭 회귀 결과)을 주입하고, ETA 임박/
정상/추세없음/표본부족 경계에서 finding이 올바르게 나는지 검증한다.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline" / "etl_collector"


def _load():
    import sys
    sys.path.insert(0, str(_ROOT))
    spec = importlib.util.spec_from_file_location(
        "capacity_forecast", _ROOT / "collectors/capacity_forecast.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


cf = _load()


def _mock_execute(max_conn, conn_regr):
    """conn_regr: dict(slope, latest, samples) — db_connections 회귀 결과.
    storage는 평탄(slope 0)으로 둬 connection 규칙만 검증."""
    def fake(rds, arn, secret, db, sql, params=None):
        if "FROM cluster_meta" in sql:
            return [{"max_connections": max_conn}]
        if "FROM cluster_settings" in sql:
            return [{"value": str(int(max_conn))}] if max_conn else []
        if "REGR_SLOPE" in sql:
            mt = params.get("mt")
            if mt == "db_connections":
                return [conn_regr]
            return [{"slope": 0.0, "latest": 2.3e9, "samples": 200}]  # storage 평탄
        if sql.strip().upper().startswith("INSERT"):
            return []
        return []
    return fake


def _run(max_conn, conn_regr):
    emitted = []
    with patch.object(cf, "_execute") as m:
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                emitted.append((params["check_type"], params["severity"], params["subject"]))
            return _mock_execute(max_conn, conn_regr)(rds, arn, secret, db, sql, params)
        m.side_effect = capture
        result = cf.collect_capacity_forecast(MagicMock(), "a", "s", "d", "c1", snapshot_ts="2026-06-11T00:00:00Z")
    return emitted, result


def test_imminent_connection_exhaustion_warns():
    # max 100, 현재 90, 하루 5씩 증가 → (100-90)/5 = 2일 후 도달 → critical
    emitted, _ = _run(100, {"slope": 5.0, "latest": 90.0, "samples": 200})
    cap = [e for e in emitted if e[0] == "capacity_forecast"]
    assert len(cap) == 1
    assert cap[0][1] == "critical"  # 2일 ≤ 3 → critical
    assert cap[0][2] == "커넥션"


def test_far_off_exhaustion_no_finding():
    # max 1716, 현재 1, 하루 0.01 증가 → 수만 일 후 → 경보 없음
    emitted, _ = _run(1716, {"slope": 0.01, "latest": 1.0, "samples": 200})
    assert [e for e in emitted if e[0] == "capacity_forecast"] == []


def test_no_growth_trend_no_finding():
    # slope ≤ 0 → 증가 추세 아님 → 침묵
    emitted, _ = _run(100, {"slope": -1.0, "latest": 50.0, "samples": 200})
    assert [e for e in emitted if e[0] == "capacity_forecast"] == []


def test_insufficient_samples_no_finding():
    # 표본 < 20 → 추세 신뢰 불가 → 침묵(거짓 경보 방지)
    emitted, _ = _run(100, {"slope": 5.0, "latest": 90.0, "samples": 5})
    assert [e for e in emitted if e[0] == "capacity_forecast"] == []


def test_unknown_max_connections_skips_connection_rule():
    # max_connections 미상 → connection ETA 계산 불가 → connection 규칙 skip
    emitted, _ = _run(None, {"slope": 5.0, "latest": 90.0, "samples": 200})
    assert [e for e in emitted if e[0] == "capacity_forecast"] == []


# ---- ACU 고갈 예측 (Serverless v2) ----
def _mock_execute_acu(max_acu, acu_agg):
    """ACU 전용 mock. storage/connection 루프는 평탄(경보 없음)으로 둔다.
    acu_agg: dict(slope, latest_peak, max_peak, days, sat_days)."""
    def fake(rds, arn, secret, db, sql, params=None):
        if "FROM cluster_meta" in sql:
            return [{"max_connections": 100, "serverlessv2_max_acu": max_acu}]
        if "serverless_acu" in sql:   # ACU 일별 peak 집계 쿼리
            return [acu_agg]
        if "REGR_SLOPE" in sql:       # storage/connection 루프 → 평탄
            return [{"slope": 0.0, "latest": 1.0, "samples": 200}]
        if "FROM cluster_settings" in sql:
            return [{"value": "100"}]
        return []
    return fake


def _run_acu(max_acu, acu_agg):
    emitted = []
    with patch.object(cf, "_execute") as m:
        def capture(rds, arn, secret, db, sql, params=None):
            if sql.strip().upper().startswith("INSERT"):
                emitted.append((params["check_type"], params["severity"], params["subject"]))
            return _mock_execute_acu(max_acu, acu_agg)(rds, arn, secret, db, sql, params)
        m.side_effect = capture
        result = cf.collect_capacity_forecast(
            MagicMock(), "a", "s", "d", "c1", snapshot_ts="2026-06-11T00:00:00Z", engine="aurora-postgresql",
        )
    return emitted, result


def test_acu_ceiling_reached_critical():
    # max 16 ACU, 5일 관측 중 4일이 95%(15.2) 이상 → 포화율 0.8 ≥ 0.6 → critical.
    emitted, _ = _run_acu(16.0, {"slope": 0.1, "latest_peak": 15.8, "max_peak": 16.0, "days": 5, "sat_days": 4})
    acu = [e for e in emitted if e[0] == "capacity_forecast" and e[2] == "ACU"]
    assert len(acu) == 1
    assert acu[0][1] == "critical"


def test_acu_trending_up_warns():
    # max 16, 일별 peak 12에서 하루 1씩 상승 → (16-12)/1 = 4일 → warning(≤7).
    # 포화일 0 → 천장 케이스 아님, 추세 케이스로 진입.
    emitted, _ = _run_acu(16.0, {"slope": 1.0, "latest_peak": 12.0, "max_peak": 12.0, "days": 6, "sat_days": 0})
    acu = [e for e in emitted if e[0] == "capacity_forecast" and e[2] == "ACU"]
    assert len(acu) == 1
    assert acu[0][1] == "warning"


def test_acu_no_max_acu_skips():
    # serverlessv2_max_acu 미상(프로비저닝) → ACU 규칙 자체를 skip.
    emitted, _ = _run_acu(None, {"slope": 1.0, "latest_peak": 12.0, "max_peak": 12.0, "days": 6, "sat_days": 0})
    assert [e for e in emitted if e[2] == "ACU"] == []


def test_acu_insufficient_days_skips():
    # 일별 peak 관측 < 3일 → 추세 신뢰 불가 → 침묵.
    emitted, _ = _run_acu(16.0, {"slope": 1.0, "latest_peak": 15.0, "max_peak": 15.0, "days": 2, "sat_days": 2})
    assert [e for e in emitted if e[2] == "ACU"] == []


def test_acu_flat_trend_no_finding():
    # 포화 아님 + slope ≤ 0 → 천장도 추세도 아님 → 침묵.
    emitted, _ = _run_acu(16.0, {"slope": -0.5, "latest_peak": 5.0, "max_peak": 6.0, "days": 6, "sat_days": 0})
    assert [e for e in emitted if e[2] == "ACU"] == []
