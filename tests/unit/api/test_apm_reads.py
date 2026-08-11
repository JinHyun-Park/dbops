import importlib.util, json, base64
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "apm_handler", Path(__file__).resolve().parents[3] / "api/apm/handler.py")


def _load():
    m = importlib.util.module_from_spec(_SPEC); _SPEC.loader.exec_module(m); return m


def _event(qs=None):
    claims = {"cognito:groups": ["dbops-admin"], "cognito:username": "h"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"requestContext": {"http": {"method": "GET"}},
            "headers": {"Authorization": f"Bearer h.{payload}.s"},
            "queryStringParameters": qs or {}}


def test_overview_shapes_latest_metrics(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_get_target", lambda t: {"target_id": t, "team": ""})
    monkeypatch.setattr(mod, "_execute", lambda sql, params=None: (
        [{"metric_type": "cpu", "value": 55.0}, {"metric_type": "latency_avg", "value": 120.0}]
        if "apm_metric_snapshots" in sql else [{"level": "ERROR", "total": 7}]))
    resp = mod._overview(_event(), "svc-a")
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["metrics"]["cpu"] == 55.0
    assert body["log_counts"]["ERROR"] == 7


def test_metrics_returns_series(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_get_target", lambda t: {"target_id": t, "team": ""})
    monkeypatch.setattr(mod, "_execute", lambda sql, params=None: [
        {"ts": "2026-08-11T00:00:00Z", "value": 1.0}])
    resp = mod._metrics(_event({"metric_type": "cpu", "hours": "3"}), "svc-a")
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["series"]
