import importlib.util
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[3] / "data-pipeline" / "outcome_evaluator"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

def _load(mod_name, file_name=None):
    spec = importlib.util.spec_from_file_location(mod_name, PKG / f"{file_name or mod_name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

case_opener = _load("case_opener")


def _capturing_query(finding_rows, anomaly_rows):
    """Returns a query() stub that answers the two SELECTs by SQL keyword and
    records every INSERT it receives."""
    inserts = []

    def query(sql, params=None):
        if "cluster_health_findings" in sql and "SELECT" in sql:
            return finding_rows
        if "event_log" in sql and "SELECT" in sql:
            return anomaly_rows
        if sql.strip().upper().startswith("INSERT INTO REMEDIATION_CASES"):
            inserts.append(params)
            return []
        return []

    query.inserts = inserts
    return query


def test_opens_finding_and_anomaly_cases():
    q = _capturing_query(
        finding_rows=[
            {
                "cluster_id": "c1",
                "check_type": "query_regression",
                "subject": "SELECT ...",
                "severity": "warning",
                "recommendation": "인덱스 점검",
                "snapshot_time": "2026-06-30T00:00:00Z",
            }
        ],
        anomaly_rows=[
            {
                "cluster_id": "c1",
                "event_type": "anomaly_cpu",
                "message": "...",
                "event_time": "2026-06-30T00:01:00Z",
            }
        ],
    )
    n = case_opener.open_cases(q)
    assert n == 2
    by_class = {p["symptom_class"]: p for p in q.inserts}
    assert by_class["finding:query_regression"]["action_class"] == "index_add"
    assert by_class["finding:query_regression"]["watch_metric"] is None
    assert by_class["anomaly:cpu"]["watch_metric"] == "cpu"
    assert by_class["anomaly:cpu"]["symptom_subject"] == "cpu"
    assert by_class["finding:query_regression"]["source"] == "finding_collector"
    assert by_class["anomaly:cpu"]["source"] == "proactive_monitor"
    assert by_class["finding:query_regression"]["severity_at_open"] == "warning"


def test_malformed_anomaly_row_opens_no_case():
    """event_type='anomaly_' (empty suffix) must not open any case."""
    q = _capturing_query(
        finding_rows=[],
        anomaly_rows=[
            {
                "cluster_id": "c1",
                "event_type": "anomaly_",
                "message": "malformed",
                "event_time": "2026-06-30T00:00:00Z",
            }
        ],
    )
    n = case_opener.open_cases(q)
    assert n == 0
    assert q.inserts == []


def test_no_rows_opens_nothing():
    q = _capturing_query([], [])
    assert case_opener.open_cases(q) == 0
