from outcome_evaluator.remediation_classify import classify_action


def test_index_and_param_and_scale():
    assert classify_action("Query Lab에서 EXPLAIN으로 플랜 확인, 필요하면 인덱스 점검") == "index_add"
    assert classify_action("work_mem 및 max_connections 파라미터를 조정하세요") == "param_change"
    assert classify_action("ACU 상한을 올려 스케일 업하세요") == "scale_up"


def test_vacuum_analyze_and_default():
    assert classify_action("autovacuum/VACUUM 점검 권장") == "vacuum"
    assert classify_action("통계가 오래됨 — ANALYZE 실행") == "analyze"
    assert classify_action("원인 불명, 수동 점검 필요") == "manual"
    assert classify_action("") == "manual"
