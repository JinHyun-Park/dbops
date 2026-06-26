"""Query latency-regression findings collector."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_C = Path(__file__).resolve().parents[3] / "data-pipeline/etl_collector/collectors/query_regression.py"
_spec = importlib.util.spec_from_file_location("query_regression", _C)
qr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qr)


def _resp(rows):
    """Raw RDS Data API response for the regression SELECT (cols → records)."""
    cols = ["query_hash", "query_text", "baseline_mean", "recent_mean", "total_calls"]
    records = [[
        {"stringValue": r["query_hash"]},
        {"stringValue": r["query_text"]},
        {"doubleValue": r["baseline_mean"]},
        {"doubleValue": r["recent_mean"]},
        {"longValue": r["total_calls"]},
    ] for r in rows]
    return {"columnMetadata": [{"name": c} for c in cols], "records": records}


def _make_rds(select_resp):
    rds = MagicMock()
    inserted = []

    def _exec(**kw):
        if "cluster_health_findings" in kw["sql"]:
            inserted.append({x["name"]: next(iter(x["value"].values()))
                             for x in kw.get("parameters", [])})
            return {"columnMetadata": [], "records": []}
        return select_resp

    rds.execute_statement.side_effect = _exec
    return rds, inserted


def test_regression_emits_warning_and_critical():
    rds, inserted = _make_rds(_resp([
        {"query_hash": "q1", "query_text": "SELECT * FROM big", "baseline_mean": 20.0,
         "recent_mean": 60.0, "total_calls": 500},   # ratio 3 → warning
        {"query_hash": "q2", "query_text": "SELECT a FROM t", "baseline_mean": 10.0,
         "recent_mean": 100.0, "total_calls": 300},   # ratio 10 → critical
    ]))
    res = qr.collect_query_regression(rds, "arn", "sec", "db", "c1", "2026-06-26T00:00:00Z")
    assert res["findings"] == 2 and res["checked"] == 2
    by_subj = {p["subject"]: p for p in inserted}
    assert by_subj["SELECT * FROM big"]["severity"] == "warning"
    assert by_subj["SELECT * FROM big"]["value_str"] == "60ms (×3.0)"
    assert by_subj["SELECT * FROM big"]["threshold_str"] == "기준 20ms"
    assert by_subj["SELECT a FROM t"]["severity"] == "critical"  # ratio 10 ≥ 5
    assert all(p["check_type"] == "query_regression" for p in inserted)


def test_regression_no_rows_no_findings():
    rds, inserted = _make_rds(_resp([]))
    res = qr.collect_query_regression(rds, "a", "s", "db", "c1", "t")
    assert res["findings"] == 0 and inserted == []


def test_regression_subject_truncated_and_hash_fallback():
    rds, inserted = _make_rds(_resp([
        {"query_hash": "h9", "query_text": "x" * 300, "baseline_mean": 10.0,
         "recent_mean": 30.0, "total_calls": 99},
    ]))
    qr.collect_query_regression(rds, "a", "s", "db", "c1", "t")
    assert len(inserted[0]["subject"]) == qr.SUBJECT_MAX  # long text truncated
