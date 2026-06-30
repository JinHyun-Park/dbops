from outcome_evaluator import evaluator


def _query_router(routes):
    """routes: list of (sql_substring, rows). First substring match answers."""
    def query(sql, params=None):
        for sub, rows in routes:
            if sub in sql:
                return rows
        return []
    return query

def test_metric_case_resolved_when_back_in_band():
    q = _query_router([
        ("AVG(value)", [{"v": 30.0}]),                       # recent avg
        ("metric_baselines", [{"median": 28.0, "iqr": 5.0}]),# 28 ± 3*5 = [13,43] → 30 in band
    ])
    case = {"cluster_id": "c1", "symptom_class": "anomaly:cpu", "symptom_subject": "cpu",
            "watch_metric": "cpu", "action_class": "manual", "opened_at": "2026-06-30T00:00:00Z"}
    assert evaluator.evaluate_case(q, case) == "resolved"

def test_metric_case_persisted_when_out_of_band():
    q = _query_router([
        ("AVG(value)", [{"v": 95.0}]),
        ("metric_baselines", [{"median": 28.0, "iqr": 5.0}]),
    ])
    case = {"cluster_id": "c1", "symptom_class": "anomaly:cpu", "symptom_subject": "cpu",
            "watch_metric": "cpu", "action_class": "manual", "opened_at": "x"}
    assert evaluator.evaluate_case(q, case) == "persisted"

def test_metric_case_inconclusive_without_baseline():
    q = _query_router([("AVG(value)", [{"v": 30.0}]), ("metric_baselines", [])])
    case = {"watch_metric": "cpu", "symptom_subject": "cpu", "cluster_id": "c1",
            "symptom_class": "anomaly:cpu", "action_class": "manual", "opened_at": "x"}
    assert evaluator.evaluate_case(q, case) == "inconclusive"

def test_finding_case_resolved_only_if_collector_ran():
    # not recurred (0) + collector ran (other findings exist → cnt>0) ⇒ resolved
    q = _query_router([
        ("AS recurred", [{"recurred": 0}]),
        ("AS produced", [{"produced": 4}]),
    ])
    case = {"cluster_id": "c1", "symptom_class": "finding:query_regression",
            "symptom_subject": "SELECT ...", "watch_metric": None,
            "action_class": "index_add", "opened_at": "x"}
    assert evaluator.evaluate_case(q, case) == "resolved"

def test_finding_case_inconclusive_when_collector_silent():
    # not recurred (0) BUT collector produced nothing → can't call it resolved
    q = _query_router([("AS recurred", [{"recurred": 0}]), ("AS produced", [{"produced": 0}])])
    case = {"cluster_id": "c1", "symptom_class": "finding:query_regression",
            "symptom_subject": "s", "watch_metric": None, "action_class": "index_add", "opened_at": "x"}
    assert evaluator.evaluate_case(q, case) == "inconclusive"

def test_apply_verdict_resolved_bumps_both_agg_rows():
    seen = []

    def query(sql, params=None):
        seen.append((sql, params))
        return []

    case = {
        "case_id": 7,
        "cluster_id": "c1",
        "symptom_class": "anomaly:cpu",
        "action_class": "manual",
    }
    evaluator.apply_verdict(query, case, "resolved")
    agg = [p for s, p in seen if "remediation_outcomes_agg" in s]
    assert {p["cluster_id"] for p in agg} == {"c1", "*"}
    assert all(p["succ_inc"] == 1 for p in agg)  # resolved ⇒ successes += 1
