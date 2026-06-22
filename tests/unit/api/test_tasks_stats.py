import importlib
import json
from unittest.mock import patch

h = importlib.import_module("api.tasks.handler")


def _evt(path="/api/tasks/stats", method="GET"):
    return {"rawPath": path, "requestContext": {"http": {"method": method}}}


def test_stats_aggregates(monkeypatch):
    rows = [
        {"status": "done", "kind": "auto_rca", "duration_ms": 400},
        {"status": "done", "kind": "manual_rca", "duration_ms": 600},
        {"status": "failed", "kind": "auto_rca"},
        {"status": "running", "kind": "scheduled_report"},
    ]
    with patch.object(h, "_recent_for_stats", return_value=rows):
        resp = h.lambda_handler(_evt(), None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["total"] == 4
    assert body["by_status"]["done"] == 2
    assert body["by_kind"]["auto_rca"] == 2
    # success_rate = done / (done+failed) finished tasks = 2/3
    assert round(body["success_rate"], 2) == 0.67
    assert body["avg_duration_ms"] == 500  # mean of done durations
    assert body["recent_failures"] == 1


def test_stats_empty_is_zero_safe(monkeypatch):
    with patch.object(h, "_recent_for_stats", return_value=[]):
        resp = h.lambda_handler(_evt(), None)
    body = json.loads(resp["body"])
    assert body["total"] == 0 and body["success_rate"] == 0 and body["avg_duration_ms"] == 0
