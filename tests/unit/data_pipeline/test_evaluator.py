import sys

from . import _oe_loader as _ldr

_PATH_ADDED = _ldr.install_path()

def _load(mod_name, file_name=None):
    return _ldr.load(mod_name, file_name)

evaluator = _load("evaluator")


def teardown_module(module):
    _ldr.teardown(_PATH_ADDED, "evaluator")


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


# F3: persisted verdict bumps attempts but NOT successes
def test_apply_verdict_persisted_does_not_bump_successes():
    seen = []

    def query(sql, params=None):
        seen.append((sql, params))
        return []

    case = {
        "case_id": 8,
        "cluster_id": "c1",
        "symptom_class": "anomaly:cpu",
        "action_class": "manual",
    }
    evaluator.apply_verdict(query, case, "persisted")
    agg = [p for s, p in seen if "remediation_outcomes_agg" in s]
    assert {p["cluster_id"] for p in agg} == {"c1", "*"}
    assert all(p["succ_inc"] == 0 for p in agg)  # persisted ⇒ successes not bumped


# F3: inconclusive skips agg entirely
def test_apply_verdict_inconclusive_skips_agg():
    seen = []

    def query(sql, params=None):
        seen.append((sql, params))
        return []

    case = {
        "case_id": 9,
        "cluster_id": "c1",
        "symptom_class": "anomaly:cpu",
        "action_class": "manual",
    }
    evaluator.apply_verdict(query, case, "inconclusive")
    agg_sqls = [s for s, _ in seen if "remediation_outcomes_agg" in s]
    assert agg_sqls == []  # zero agg writes for inconclusive


# F4: agg upserts must appear before the case UPDATE (atomicity ordering)
def test_apply_verdict_writes_agg_before_marking_case():
    seen = []

    def query(sql, params=None):
        seen.append(sql)
        return []

    case = {
        "case_id": 10,
        "cluster_id": "c1",
        "symptom_class": "anomaly:cpu",
        "action_class": "manual",
    }
    evaluator.apply_verdict(query, case, "resolved")
    first_agg_idx = next(i for i, s in enumerate(seen) if "remediation_outcomes_agg" in s)
    case_update_idx = next(i for i, s in enumerate(seen) if "UPDATE remediation_cases" in s)
    assert first_agg_idx < case_update_idx  # agg writes precede case status update


# F5: symptom_class with no colon returns inconclusive (does not raise)
def test_evaluate_case_no_colon_symptom_class_is_inconclusive():
    q = _query_router([])  # no DB rows needed — should short-circuit before querying
    case = {"cluster_id": "c1", "symptom_class": "legacy_no_colon",
            "symptom_subject": "s", "watch_metric": None,
            "action_class": "manual", "opened_at": "x"}
    assert evaluator.evaluate_case(q, case) == "inconclusive"
