import importlib.util, json, base64
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "apm_handler", Path(__file__).resolve().parents[3] / "api/apm/handler.py")


def _load():
    m = importlib.util.module_from_spec(_SPEC); _SPEC.loader.exec_module(m); return m


def _event(body):
    claims = {"cognito:groups": ["dbops-admin"], "cognito:username": "h"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"requestContext": {"http": {"method": "POST"}},
            "headers": {"Authorization": f"Bearer h.{payload}.s"},
            "body": json.dumps(body)}


def test_levels_filter_defaults_to_error_warn():
    mod = _load()
    clause = mod._levels_filter(None)
    assert "ERROR" in clause and "WARN" in clause
    assert "INFO" not in clause


def test_levels_filter_honors_explicit_levels():
    mod = _load()
    clause = mod._levels_filter(["INFO"])
    assert "INFO" in clause and "ERROR" not in clause


def test_logs_search_runs_query(monkeypatch):
    import time
    mod = _load()
    monkeypatch.setattr(mod, "_get_target", lambda t: {
        "target_id": t, "team": "", "region": "ap-northeast-2",
        "spoke_role_arn": "", "log_groups": ["/app/orders"]})

    captured = {}

    class FakeLogs:
        def start_query(self, **kw):
            assert "ERROR" in kw["queryString"]  # default filter applied
            # Must be epoch seconds, not milliseconds (< 10_000_000_000)
            assert kw["startTime"] < 10_000_000_000, "startTime must be epoch seconds, not milliseconds"
            assert kw["endTime"] < 10_000_000_000, "endTime must be epoch seconds, not milliseconds"
            captured.update(kw)
            return {"queryId": "q1"}
        def get_query_results(self, **kw):
            return {"status": "Complete", "results": [
                [{"field": "@timestamp", "value": "2026-08-11 00:00"},
                 {"field": "@message", "value": "ERROR boom"}]]}

    monkeypatch.setattr(mod, "_logs_client_for", lambda item: FakeLogs())
    resp = mod._logs_search(_event({"log_group": "/app/orders"}), "svc-a")
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["count"] == 1
    assert body["entries"][0]["message"] == "ERROR boom"


def test_levels_filter_all_via_flag_returns_empty():
    mod = _load()
    assert mod._levels_filter(None, all_levels=True) == ""


def test_levels_filter_explicit_empty_list_returns_empty():
    mod = _load()
    assert mod._levels_filter([], all_levels=False) == ""


def test_levels_filter_absent_still_defaults_error_warn():
    mod = _load()
    clause = mod._levels_filter(None)
    assert "ERROR" in clause and "WARN" in clause and "INFO" not in clause


def test_resolve_window_minutes():
    mod = _load()
    s, e, clamped = mod._resolve_window({"minutes": 5}, now=1_000_000)
    assert e == 1_000_000 and s == 1_000_000 - 300 and clamped is False


def test_resolve_window_absolute():
    mod = _load()
    s, e, clamped = mod._resolve_window({"start": 100, "end": 700}, now=1_000_000)
    assert s == 100 and e == 700 and clamped is False


def test_resolve_window_absolute_over_48h_clamps():
    mod = _load()
    span = 60 * 3600  # 60h
    s, e, clamped = mod._resolve_window({"start": 0, "end": span}, now=1_000_000)
    assert e == span and s == span - 48 * 3600 and clamped is True


def test_resolve_window_bad_absolute_falls_back_to_hours():
    mod = _load()
    # start >= end is invalid -> fall through to hours
    s, e, clamped = mod._resolve_window({"start": 900, "end": 100, "hours": 2}, now=1_000_000)
    assert e == 1_000_000 and s == 1_000_000 - 2 * 3600 and clamped is False


def test_resolve_window_default_one_hour():
    mod = _load()
    s, e, clamped = mod._resolve_window({}, now=1_000_000)
    assert e == 1_000_000 and s == 1_000_000 - 3600 and clamped is False


def test_resolve_window_hours_clamped_1_to_48():
    mod = _load()
    s, _, _ = mod._resolve_window({"hours": 999}, now=1_000_000)
    assert s == 1_000_000 - 48 * 3600
