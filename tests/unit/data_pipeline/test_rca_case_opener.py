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
        """pages: list of lists (each inner list = one scan page's Items)."""
        self._pages = list(pages)
        self._call = 0

    def scan(self, **kw):
        page = self._pages[self._call % len(self._pages)]
        self._call += 1
        result = {"Items": page}
        # Signal pagination for every page except the last
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


def test_opens_rca_case_with_inferred_action():
    q = _capturing_query()
    tbl = _FakeTable([[
        {
            "task_id": "t1", "kind": "auto_rca", "status": "done", "cluster_id": "c1",
            "completed_at": "9999999999999",
            "result": {
                "candidates": [{"category": "lock_contention", "metric": "blocking_count"}],
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


def test_pagination_collects_all_pages():
    q = _capturing_query()
    page1 = [
        {
            "task_id": "t2", "kind": "manual_rca", "status": "done", "cluster_id": "c2",
            "completed_at": "9999999999999",
            "result": {
                "candidates": [{"category": "high_cpu", "metric": "cpu_utilization"}],
                "recommendations": ["파라미터를 튜닝하세요"],
            },
        }
    ]
    page2 = [
        {
            "task_id": "t3", "kind": "auto_rca", "status": "done", "cluster_id": "c3",
            "completed_at": "9999999999999",
            "result": {
                "candidates": [{"category": "slow_query", "metric": None}],
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
    assert n == 2
    classes = {p["symptom_class"] for p in q.inserts}
    assert "rca:high_cpu" in classes
    assert "rca:slow_query" in classes


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


def test_missing_metric_becomes_none():
    q = _capturing_query()
    tbl = _FakeTable([[
        {
            "task_id": "t7", "kind": "auto_rca", "status": "done", "cluster_id": "c1",
            "completed_at": "9999999999999",
            "result": {
                "candidates": [{"category": "slow_query"}],  # no "metric" key
                "recommendations": ["인덱스를 추가하세요"],
            },
        }
    ]])
    n = case_opener.open_rca_cases(q, tbl)
    assert n == 1
    assert q.inserts[0]["watch_metric"] is None
