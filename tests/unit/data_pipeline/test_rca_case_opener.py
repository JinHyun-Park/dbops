import sys

from . import _oe_loader as _ldr

_PATH_ADDED = _ldr.install_path()

def _load(mod_name, file_name=None):
    return _ldr.load(mod_name, file_name)

_load("remediation_classify")
case_opener = _load("case_opener")


def teardown_module(module):
    _ldr.teardown(_PATH_ADDED, "remediation_classify", "case_opener")


class _FakeTable:
    def __init__(self, pages):
        """pages: list of lists (each inner list = one scan page's Items).
        Records every scan call's kwargs for assertion."""
        self._pages = list(pages)
        self._call = 0
        self.calls = []  # list of kwargs dicts passed to each scan()

    def scan(self, **kw):
        self.calls.append(dict(kw))
        page = self._pages[self._call]
        self._call += 1
        result = {"Items": page}
        # Emit LastEvaluatedKey for all pages except the last — terminates correctly
        if self._call < len(self._pages):
            result["LastEvaluatedKey"] = {"task_id": {"S": f"page{self._call}"}}
        return result


def _capturing_query():
    inserts = []

    def query(sql, params=None):
        if sql.strip().upper().startswith("INSERT INTO REMEDIATION_CASES"):
            inserts.append(params)
        return []

    query.inserts = inserts
    return query


def test_completed_at_filter_is_passed_and_no_limit():
    """Scan must carry a completed_at FilterExpression and must NOT carry a Limit."""
    import time
    q = _capturing_query()
    item = {
        "task_id": "t0", "kind": "auto_rca", "status": "done", "cluster_id": "c0",
        "completed_at": str(int(time.time() * 1000)),
        "result": {
            # Real candidate shape: metric lives at evidence.metric_type
            "candidates": [{"category": "lock_contention", "evidence": {"metric_type": "blocking_count"}}],
            "recommendations": ["인덱스를 추가하세요"],
        },
    }
    tbl = _FakeTable([[item]])
    case_opener.open_rca_cases(q, tbl)

    assert len(tbl.calls) >= 1, "scan was never called"
    first_call = tbl.calls[0]
    # FilterExpression must be present
    assert "FilterExpression" in first_call, "completed_at FilterExpression not passed to scan"
    # Limit must NOT be set (repo gotcha: Limit + FilterExpression silently drops rows)
    assert "Limit" not in first_call, "Limit must not be passed alongside FilterExpression"


def test_fakeable_pagination_terminates_and_collects_all():
    """_FakeTable with 2 pages returns all items exactly once (not infinite loop)."""
    q = _capturing_query()
    page1 = [{
        "task_id": "tp1", "kind": "auto_rca", "status": "done", "cluster_id": "c1",
        "completed_at": "9999999999999",
        "result": {"candidates": [{"category": "cat_a", "evidence": {"metric_type": "m_a"}}], "recommendations": ["fix a"]},
    }]
    page2 = [{
        "task_id": "tp2", "kind": "auto_rca", "status": "done", "cluster_id": "c2",
        "completed_at": "9999999999999",
        "result": {"candidates": [{"category": "cat_b", "evidence": {"metric_type": "m_b"}}], "recommendations": ["fix b"]},
    }]
    tbl = _FakeTable([page1, page2])
    n = case_opener.open_rca_cases(q, tbl)
    assert n == 2
    assert tbl._call == 2  # exactly 2 pages scanned, not stuck in a loop
    classes = {p["symptom_class"] for p in q.inserts}
    assert "rca:cat_a" in classes
    assert "rca:cat_b" in classes


def test_opens_rca_case_with_inferred_action():
    """Metric-backed candidate (evidence.metric_type set) opens a case with correct fields."""
    q = _capturing_query()
    tbl = _FakeTable([[
        {
            "task_id": "t1", "kind": "auto_rca", "status": "done", "cluster_id": "c1",
            "completed_at": "9999999999999",
            "result": {
                # Real shape: metric lives at evidence.metric_type, NOT top-level "metric"
                "candidates": [{"category": "lock_contention", "evidence": {"metric_type": "blocking_count"}}],
                "recommendations": ["인덱스를 추가해 잠금 경합을 줄이세요"],
            },
        }
    ]])
    n = case_opener.open_rca_cases(q, tbl)
    assert n == 1
    p = q.inserts[0]
    assert p["symptom_class"] == "rca:lock_contention"
    assert p["action_class"] == "index_add"
    assert p["watch_metric"] == "blocking_count"


def test_non_metric_candidate_opens_no_case():
    """Non-metric RCA (no evidence.metric_type) must NOT open a case — false-resolved guard."""
    q = _capturing_query()
    tbl = _FakeTable([[
        {
            "task_id": "t1nm", "kind": "auto_rca", "status": "done", "cluster_id": "c1",
            "completed_at": "9999999999999",
            "result": {
                # blocking category with evidence that has NO metric_type (e.g. only blocking_query)
                "candidates": [{"category": "blocking", "evidence": {"blocking_query": "SELECT 1"}}],
                "recommendations": ["킬 쿼리를 실행하세요"],
            },
        }
    ]])
    n = case_opener.open_rca_cases(q, tbl)
    assert n == 0, "non-metric RCA must not open a case (no resolution signal)"
    assert q.inserts == []


def test_pagination_collects_all_pages():
    """Pagination works across pages; non-metric candidate (slow_query page) is skipped."""
    q = _capturing_query()
    page1 = [
        {
            "task_id": "t2", "kind": "manual_rca", "status": "done", "cluster_id": "c2",
            "completed_at": "9999999999999",
            "result": {
                "candidates": [{"category": "high_cpu", "evidence": {"metric_type": "cpu_utilization"}}],
                "recommendations": ["파라미터를 튜닝하세요"],
            },
        }
    ]
    page2 = [
        {
            "task_id": "t3", "kind": "auto_rca", "status": "done", "cluster_id": "c3",
            "completed_at": "9999999999999",
            "result": {
                # slow_query has no metric_type in evidence → skipped
                "candidates": [{"category": "slow_query", "evidence": {}}],
                "recommendations": ["인덱스를 추가하세요"],
            },
        }
    ]

    # Two-page table: manually control pagination
    call_count = [0]
    def fake_scan(**kw):
        call_count[0] += 1
        if call_count[0] == 1:
            return {"Items": page1, "LastEvaluatedKey": {"task_id": {"S": "t3"}}}
        return {"Items": page2}

    class _ManualPaginated:
        def scan(self, **kw):
            return fake_scan(**kw)

    n = case_opener.open_rca_cases(q, _ManualPaginated())
    # only high_cpu (metric-backed) opens a case; slow_query (no metric) is skipped
    assert n == 1
    classes = {p["symptom_class"] for p in q.inserts}
    assert "rca:high_cpu" in classes
    assert "rca:slow_query" not in classes


def test_non_rca_kinds_skipped():
    q = _capturing_query()
    tbl = _FakeTable([[
        {
            "task_id": "t4", "kind": "scheduled_task", "status": "done", "cluster_id": "c1",
            "completed_at": "9999999999999",
            "result": {"candidates": [{"category": "lock_contention"}], "recommendations": []},
        }
    ]])
    assert case_opener.open_rca_cases(q, tbl) == 0
    assert q.inserts == []


def test_non_done_status_skipped():
    q = _capturing_query()
    tbl = _FakeTable([[
        {
            "task_id": "t5", "kind": "auto_rca", "status": "pending", "cluster_id": "c1",
            "completed_at": "9999999999999",
            "result": {"candidates": [{"category": "lock_contention"}], "recommendations": []},
        }
    ]])
    assert case_opener.open_rca_cases(q, tbl) == 0


def test_no_candidates_skipped():
    q = _capturing_query()
    tbl = _FakeTable([[
        {
            "task_id": "t6", "kind": "auto_rca", "status": "done", "cluster_id": "c1",
            "completed_at": "9999999999999",
            "result": {"candidates": [], "recommendations": ["뭔가 해보세요"]},
        }
    ]])
    assert case_opener.open_rca_cases(q, tbl) == 0


def test_none_table_returns_zero():
    q = _capturing_query()
    assert case_opener.open_rca_cases(q, None) == 0


def test_metric_backed_non_top_candidate_opens_case():
    """Top candidate is non-metric (blocking); second candidate is metric-backed.
    Case must open anchored to the metric-backed candidate, not the top one."""
    q = _capturing_query()
    tbl = _FakeTable([[
        {
            "task_id": "t_mixed", "kind": "auto_rca", "status": "done", "cluster_id": "c9",
            "completed_at": "9999999999999",
            "result": {
                "candidates": [
                    # rank-1: non-metric (blocking) — no metric_type in evidence
                    {"category": "blocking", "evidence": {"blocking_query": "SELECT 1 FOR UPDATE"}},
                    # rank-2: metric-backed — should be chosen
                    {"category": "metric_spike", "evidence": {"metric_type": "cpu"}},
                ],
                "recommendations": ["CPU 사용량을 줄이세요"],
            },
        }
    ]])
    n = case_opener.open_rca_cases(q, tbl)
    assert n == 1, "should open exactly 1 case anchored to the metric-backed candidate"
    p = q.inserts[0]
    assert p["symptom_class"] == "rca:metric_spike"
    assert p["watch_metric"] == "cpu"


def test_all_non_metric_candidates_opens_no_case():
    """All candidates are non-metric — must open 0 cases."""
    q = _capturing_query()
    tbl = _FakeTable([[
        {
            "task_id": "t_allnm", "kind": "auto_rca", "status": "done", "cluster_id": "c10",
            "completed_at": "9999999999999",
            "result": {
                "candidates": [
                    {"category": "blocking", "evidence": {"blocking_query": "SELECT 1"}},
                    {"category": "slow_query", "evidence": {}},
                ],
                "recommendations": ["쿼리를 최적화하세요"],
            },
        }
    ]])
    n = case_opener.open_rca_cases(q, tbl)
    assert n == 0
    assert q.inserts == []


def test_missing_evidence_metric_type_skips_case():
    """Candidate with no evidence.metric_type must be skipped — no false-resolved cases."""
    q = _capturing_query()
    tbl = _FakeTable([[
        {
            "task_id": "t7", "kind": "auto_rca", "status": "done", "cluster_id": "c1",
            "completed_at": "9999999999999",
            "result": {
                # No evidence key at all — metric_type is absent
                "candidates": [{"category": "slow_query"}],
                "recommendations": ["인덱스를 추가하세요"],
            },
        }
    ]])
    n = case_opener.open_rca_cases(q, tbl)
    assert n == 0, "candidate without evidence.metric_type must not open a case"
    assert q.inserts == []
