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
        "pg_capacity_forecast", _ROOT / "collectors/pg_capacity_forecast.py"
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
