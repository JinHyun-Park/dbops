from unittest.mock import patch

from outcome_evaluator import handler


def test_opens_then_evaluates_due_cases(monkeypatch):
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:cluster")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:secret")
    due = [{"case_id": 1, "cluster_id": "c1", "symptom_class": "anomaly:cpu",
            "symptom_subject": "cpu", "watch_metric": "cpu", "action_class": "manual",
            "opened_at": "x"}]
    with patch.object(handler, "_query", return_value=None) as mq, \
         patch.object(handler.case_opener, "open_cases", return_value=3) as mo, \
         patch.object(handler, "_due_cases", return_value=due), \
         patch.object(handler.evaluator, "evaluate_case", return_value="resolved") as me, \
         patch.object(handler.evaluator, "apply_verdict") as ma:
        out = handler.lambda_handler({}, None)
    assert out == {"opened": 3, "evaluated": 1, "failed": 0}
    mo.assert_called_once()
    me.assert_called_once()
    ma.assert_called_once()


def test_opener_failure_still_evaluates(monkeypatch):
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:cluster")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:secret")
    due = [{"case_id": 2, "cluster_id": "c1", "symptom_class": "anomaly:cpu",
            "symptom_subject": "cpu", "watch_metric": "cpu", "action_class": "manual",
            "opened_at": "x"}]
    with patch.object(handler.case_opener, "open_cases", side_effect=RuntimeError("db down")), \
         patch.object(handler, "_due_cases", return_value=due), \
         patch.object(handler.evaluator, "evaluate_case", return_value="resolved"), \
         patch.object(handler.evaluator, "apply_verdict"):
        out = handler.lambda_handler({}, None)
    assert out["opened"] == 0
    assert out["evaluated"] == 1


def test_per_case_failure_counted_and_isolated(monkeypatch):
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:cluster")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:secret")
    due = [
        {"case_id": 10, "cluster_id": "c1", "symptom_class": "anomaly:cpu",
         "symptom_subject": "cpu", "watch_metric": "cpu", "action_class": "manual",
         "opened_at": "x"},
        {"case_id": 11, "cluster_id": "c1", "symptom_class": "anomaly:cpu",
         "symptom_subject": "cpu", "watch_metric": "cpu", "action_class": "manual",
         "opened_at": "x"},
    ]
    with patch.object(handler.case_opener, "open_cases", return_value=0), \
         patch.object(handler, "_due_cases", return_value=due), \
         patch.object(handler.evaluator, "evaluate_case",
                      side_effect=[RuntimeError("parse error"), "resolved"]), \
         patch.object(handler.evaluator, "apply_verdict"):
        out = handler.lambda_handler({}, None)
    assert out["failed"] == 1
    assert out["evaluated"] == 1
