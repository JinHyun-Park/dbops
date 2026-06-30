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
    assert out == {"opened": 3, "evaluated": 1}
    mo.assert_called_once()
    me.assert_called_once()
    ma.assert_called_once()
