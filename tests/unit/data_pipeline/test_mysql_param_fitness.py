"""MySQL Parameter Fitness 진단 단위 테스트.

collect_mysql_param_fitness는 RDS Data API에 의존하므로 _execute를 모킹해
캐시 응답(메타·MySQL global variables·메트릭)을 주입하고 emit된 finding을
확인한다. 핵심은 per-connection 버퍼 × max_connections OOM 상호작용 규칙.

IDENTIFIER PINNING (아래 _ROUTES). 이 파일의 더블은 예전에 `"FROM cluster_meta"
in sql` 같은 부분문자열로 분기했다. 그래서 `cluster_meta` → `cluster_metaZZZ`로
바꿔도 부분문자열이 그대로 남아 캔드 로우가 계속 반환됐고(MEASURED: 이 파일의
mutation 3건 모두 통과, 전체 2615개 스위트도 통과) 실행되면 반드시 깨지는 SQL이
초록으로 나갔다. 이제 더블은 각 statement가 명명해야 하는 테이블·컬럼·별칭을
정규식으로 확인하고, 모르는 SQL은 캔드 로우 대신 AssertionError를 낸다.
반쪽 정보가 아니라 양쪽 다 확보한 상태다: 여기서 식별자를 고정하고,
tests/unit/test_mysql_tier_cache_sql_real_pg.py가 같은 statement를 실제
PostgreSQL에 실행한다.
"""

import importlib.util
import re
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


# 각 statement가 반드시 명명해야 하는 식별자. \b 덕분에 `cluster_metaZZZ`는
# `cluster_meta` 패턴을 만족하지 못한다. 별칭(AS cw_hit ...)도 고정한다: 리더가
# hr["cw_hit"]로 읽으므로 별칭이 바뀌면 실제로는 전부 None이 된다.
# 클러스터 레벨 집계의 strict dimension 필터(dimensions::text = '{}')도 고정한다.
_ROUTES = (
    ("meta", r"SELECT\s+instance_class,\s*engine_mode,\s*serverlessv2_max_acu,\s*engine\s+"
             r"FROM\s+cluster_meta\b\s+WHERE\s+cluster_id\b"),
    ("settings", r"SELECT\s+name,\s*value\s+FROM\s+cluster_settings\b\s+WHERE\s+cluster_id\b"),
    ("conn", r"MAX\(value\)\s+AS\s+peak\b.*COUNT\(\*\)\s+AS\s+samples\b.*"
             r"FROM\s+metric_snapshots\b.*metric_type\s*=\s*'db_connections'.*"
             r"\bts\s*>\s*NOW\(\).*dimensions::text\s*=\s*'\{\}'"),
    ("hit", r"AS\s+cw_hit\b.*AS\s+cw_samples\b.*AS\s+innodb_hit\b.*AS\s+innodb_samples\b.*"
            r"FROM\s+metric_snapshots\b.*"
            r"metric_type\s+IN\s*\('buffer_cache_hit',\s*'innodb_buffer_pool_hit_rate'\).*"
            r"\bts\s*>\s*NOW\(\).*dimensions::text\s*=\s*'\{\}'"),
    ("insert", r"INSERT\s+INTO\s+cluster_health_findings\s*\(\s*cluster_id,\s*snapshot_time,\s*"
               r"check_type,\s*severity,\s*subject,\s*value_str,\s*threshold_str,\s*"
               r"recommendation,\s*details\s*\)"),
)


def _route(sql):
    for name, pat in _ROUTES:
        if re.search(pat, sql, re.S | re.I):
            return name
    raise AssertionError(
        "the collector issued SQL this double does not recognise, so no canned row "
        "can stand in for it. If an identifier changed ON PURPOSE, update _ROUTES "
        "(and the real-PostgreSQL test) instead of loosening the match:\n" + sql)


def _mock_execute(meta, settings, metrics):
    """_execute 호출을 식별자로 분기. settings는 {name: value} (MySQL은 바이트).

    캐시 히트율 쿼리는 E-3에서 두 metric_type을 한 번에 재는 4-컬럼 shape가 됐다.
    `avg_hit`/`hit_samples`는 Aurora CW 경로(buffer_cache_hit),
    `innodb_hit`/`innodb_samples`는 rds_instance 경로(innodb_buffer_pool_hit_rate).
    """
    def fake(rds, arn, secret, db, sql, params=None):
        route = _route(sql)
        if route == "meta":
            return [meta]
        if route == "settings":
            return [{"name": n, "value": v} for n, v in settings.items()]
        if route == "conn":
            return [{"peak": metrics.get("peak_conn"), "samples": metrics.get("conn_samples", 0)}]
        if route == "hit":
            return [{
                "cw_hit": metrics.get("avg_hit"),
                "cw_samples": metrics.get("hit_samples", 0),
                "innodb_hit": metrics.get("innodb_hit"),
                "innodb_samples": metrics.get("innodb_samples", 0),
            }]
        return []
    return fake


def _run(meta, settings, metrics):
    emitted = []
    rows = []
    sqls = []
    with patch.object(pf, "_execute") as m:
        def capture(rds, arn, secret, db, sql, params=None):
            sqls.append(sql)
            if sql.strip().upper().startswith("INSERT"):
                emitted.append(params["check_type"])
                rows.append(params)
            return _mock_execute(meta, settings, metrics)(rds, arn, secret, db, sql, params)
        m.side_effect = capture
        result = pf.collect_mysql_param_fitness(
            MagicMock(), "arn", "secret", "db", "c1", snapshot_ts="2026-06-11T00:00:00Z"
        )
    result["_rows"] = rows
    result["_sql"] = sqls
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


# ---------------------------------------------------------------------------
# E-3: the cache-hit rule (M3) and the engine-dependent wording.
#
# MEASURED on the live dev cache (7-day window, cluster-level rows only):
#   dbops-demo-mysql (rds_instance)  buffer_cache_hit samples = 0,
#                                    innodb_buffer_pool_hit_rate samples = 1948
#   Aurora MySQL sample cluster      buffer_cache_hit samples = 10070,
#                                    innodb_buffer_pool_hit_rate samples = 1859
# So the rule read a metric_type that NO rds_instance collector writes and was
# permanently dead for that family, while Aurora had data all along.
# ---------------------------------------------------------------------------

_AURORA_META = {"instance_class": "db.r6g.large", "engine_mode": "provisioned",
                "serverlessv2_max_acu": None, "engine": "aurora-mysql"}
_RDS_META = {"instance_class": "db.r6g.large", "engine_mode": "provisioned",
             "serverlessv2_max_acu": None, "engine": "mysql"}
_SMALL_BUFFERS = {"max_connections": "200", "sort_buffer_size": str(256 * 1024),
                  "innodb_buffer_pool_size": str(128 * _MB)}


def test_cache_hit_rule_fires_for_rds_instance_from_innodb_metric():
    """The rds_instance family only writes innodb_buffer_pool_hit_rate, so the
    rule must read it. Before E-3 this emitted NOTHING no matter how bad the
    cache hit rate was."""
    metrics = {"peak_conn": 80, "conn_samples": 100,
               "avg_hit": None, "hit_samples": 0,       # buffer_cache_hit: absent
               "innodb_hit": 80.0, "innodb_samples": 500}
    emitted, result = _run(_RDS_META, _SMALL_BUFFERS, metrics)
    assert "param_buffer_cache_hit" in emitted
    (row,) = [r for r in result["_rows"] if r["check_type"] == "param_buffer_cache_hit"]
    assert "80.0%" in row["value_str"]
    assert '"metric_type": "innodb_buffer_pool_hit_rate"' in row["details"]


def test_the_window_wording_is_derived_from_the_window_that_was_measured():
    """The M1 and M3 statements said INTERVAL '7 days' while their findings said
    "7일" as separate hardcoded literals. Widening the window therefore left the
    finding claiming a 7-day average of a longer measurement, and NOTHING failed:
    MEASURED pre-fix with the interval edited to 30 days, value_str stayed
    "80.0% (7일 평균)" and the full 2634-test suite stayed green.

    Both halves now come from WINDOW_DAYS, so this drives the constant to a
    different value and requires the SQL and the wording to move together. It
    would also catch the reverse mistake (wording derived, SQL still literal)."""
    metrics = {"peak_conn": 10, "conn_samples": 100,
               "avg_hit": None, "hit_samples": 0,
               "innodb_hit": 80.0, "innodb_samples": 500}
    with patch.object(pf, "WINDOW_DAYS", 30):
        emitted, result = _run(_RDS_META, _SMALL_BUFFERS, metrics)

    measured = [s for s in result["_sql"] if "metric_snapshots" in s]
    assert len(measured) == 2, "M1 peak-connections and M3 cache-hit windows"
    for sql in measured:
        assert "INTERVAL '30 days'" in sql, sql
        assert "'7 days'" not in sql, sql

    (hit,) = [r for r in result["_rows"] if r["check_type"] == "param_buffer_cache_hit"]
    assert hit["value_str"] == "80.0% (30일 평균)"
    assert "30일 평균" in hit["recommendation"]
    (conn,) = [r for r in result["_rows"] if r["check_type"] == "param_max_connections"]
    assert "30일 peak" in conn["threshold_str"]
    assert "최근 30일" in conn["recommendation"]
    for row in (hit, conn):
        assert "7일" not in row["value_str"] + row["threshold_str"] + row["recommendation"]


def test_cache_hit_rule_prefers_the_cloudwatch_metric_for_aurora():
    """RELATIONAL REGRESSION PIN: Aurora has BOTH metric_types. The two are
    different measurements, so they must not be averaged together: the CW value
    is used verbatim, exactly as before E-3."""
    metrics = {"peak_conn": 80, "conn_samples": 100,
               "avg_hit": 90.0, "hit_samples": 1000,     # CloudWatch
               "innodb_hit": 60.0, "innodb_samples": 500}  # InnoDB status
    emitted, result = _run(_AURORA_META, _SMALL_BUFFERS, metrics)
    assert "param_buffer_cache_hit" in emitted
    (row,) = [r for r in result["_rows"] if r["check_type"] == "param_buffer_cache_hit"]
    assert "90.0%" in row["value_str"]          # NOT 60.0, NOT the 75.0 mean
    assert '"metric_type": "buffer_cache_hit"' in row["details"]


def test_cache_hit_rule_still_silent_with_too_few_samples():
    """A low value from a handful of samples must stay silent in BOTH paths:
    an under-sampled window is not evidence."""
    metrics = {"peak_conn": 80, "conn_samples": 100,
               "avg_hit": None, "hit_samples": 0,
               "innodb_hit": 50.0, "innodb_samples": 5}
    emitted, _ = _run(_RDS_META, _SMALL_BUFFERS, metrics)
    assert "param_buffer_cache_hit" not in emitted


def test_aurora_does_not_fall_back_to_the_innodb_metric_when_its_window_is_thin():
    """The fallback is gated to the family that needed it.

    Aurora MySQL writes innodb_buffer_pool_hit_rate too (the relational branch of
    etl_collector/handler.py runs collect_mysql_innodb_status), so an ungated
    fallback fires this rule on Aurora clusters whose CloudWatch window is under
    MIN_SAMPLES, where it was previously silent. MEASURED across three revisions,
    Aurora with cw_samples=5 and innodb_samples=25 @80%:
      pre-E3 (1c8c3bf~1) -> silent
      1c8c3bf            -> fires, "80.0% (7일 평균)" from innodb_buffer_pool_hit_rate
      now                -> silent again
    The two are different measurements (CloudWatch period average vs the last
    SHOW ENGINE INNODB STATUS interval), so substituting one for the other is a
    change of answer, not a repair."""
    metrics = {"peak_conn": 80, "conn_samples": 100,
               "avg_hit": 99.9, "hit_samples": 5,        # CW present but thin
               "innodb_hit": 80.0, "innodb_samples": 25}  # would breach the floor
    emitted, _ = _run(_AURORA_META, _SMALL_BUFFERS, metrics)
    assert "param_buffer_cache_hit" not in emitted

    # No CW rows at all: same answer for Aurora, and the rds_instance family
    # still gets the rule it was missing.
    metrics_no_cw = dict(metrics, avg_hit=None, hit_samples=0)
    assert "param_buffer_cache_hit" not in _run(
        _AURORA_META, _SMALL_BUFFERS, metrics_no_cw)[0]
    assert "param_buffer_cache_hit" in _run(
        _RDS_META, _SMALL_BUFFERS, metrics_no_cw)[0]


def test_cache_hit_rule_silent_when_neither_metric_has_data():
    metrics = {"peak_conn": 80, "conn_samples": 100,
               "avg_hit": None, "hit_samples": 0,
               "innodb_hit": None, "innodb_samples": 0}
    emitted, _ = _run(_RDS_META, _SMALL_BUFFERS, metrics)
    assert "param_buffer_cache_hit" not in emitted


def test_recommendation_does_not_tell_rds_mysql_that_aurora_sizes_its_buffer_pool():
    """The measured falsehood E-3 removes: on standalone RDS MySQL
    innodb_buffer_pool_size IS the parameter to tune (MEASURED 128 MB on a 1 GB
    db.t4g.micro), and the text used to say Aurora manages it automatically."""
    metrics = {"peak_conn": 80, "conn_samples": 100,
               "avg_hit": None, "hit_samples": 0,
               "innodb_hit": 80.0, "innodb_samples": 500}
    _, result = _run(_RDS_META, _SMALL_BUFFERS, metrics)
    (row,) = [r for r in result["_rows"] if r["check_type"] == "param_buffer_cache_hit"]
    rec = row["recommendation"]
    # The removed claim: that the buffer pool is auto-sized here, so tuning the
    # parameter is pointless. Aurora may still be NAMED as a contrast, which is
    # true and useful; what must be gone is the advice built on that claim.
    assert "자동 설정하므로" not in rec
    assert "파라미터 조정보다" not in rec
    assert "Serverless v2" not in rec          # no ACU knob on a DB instance
    assert "innodb_buffer_pool_size" in rec    # named as the thing to tune
    assert "Aurora처럼 자동으로 크게 잡히지 않습니다" in rec


def test_recommendation_keeps_the_aurora_wording_for_aurora():
    metrics = {"peak_conn": 80, "conn_samples": 100,
               "avg_hit": 80.0, "hit_samples": 1000,
               "innodb_hit": None, "innodb_samples": 0}
    _, result = _run(_AURORA_META, _SMALL_BUFFERS, metrics)
    (row,) = [r for r in result["_rows"] if r["check_type"] == "param_buffer_cache_hit"]
    assert "Aurora MySQL" in row["recommendation"]
    assert "Serverless v2" in row["recommendation"]


def test_conn_buffer_recommendation_is_engine_aware():
    """The other Aurora-worded string: the ~75% auto-sizing claim must not be
    made to a standalone instance, where the buffer pool is an explicit setting."""
    big = {"max_connections": "1000", "sort_buffer_size": str(4 * _MB),
           "join_buffer_size": str(4 * _MB), "read_buffer_size": str(2 * _MB),
           "read_rnd_buffer_size": str(2 * _MB), "thread_stack": str(1 * _MB),
           "innodb_buffer_pool_size": str(128 * _MB)}
    metrics = {"peak_conn": 500, "conn_samples": 100,
               "avg_hit": 99.5, "hit_samples": 100,
               "innodb_hit": None, "innodb_samples": 0}

    _, rds_result = _run(_RDS_META, big, metrics)
    (rds_row,) = [r for r in rds_result["_rows"]
                  if r["check_type"] == "param_mysql_conn_buffers"]
    assert "~75%" not in rds_row["recommendation"]
    assert "innodb_buffer_pool_size" in rds_row["recommendation"]

    _, aurora_result = _run(_AURORA_META, big, metrics)
    (aurora_row,) = [r for r in aurora_result["_rows"]
                     if r["check_type"] == "param_mysql_conn_buffers"]
    assert "~75%" in aurora_row["recommendation"]
