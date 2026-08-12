import base64
import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "apm_handler", Path(__file__).resolve().parents[3] / "api/apm/handler.py")


def _load():
    m = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(m)
    return m


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


def _fake_target_monkeypatch(mod, monkeypatch):
    monkeypatch.setattr(mod, "_get_target", lambda t: {
        "target_id": t, "team": "", "region": "ap-northeast-2",
        "spoke_role_arn": "", "log_groups": ["/app/orders"]})


def test_logs_search_all_levels_has_no_level_filter(monkeypatch):
    mod = _load()
    _fake_target_monkeypatch(mod, monkeypatch)
    captured = {}

    class FakeLogs:
        def start_query(self, **kw):
            captured.update(kw)
            return {"queryId": "q1"}
        def get_query_results(self, **kw):
            return {"status": "Complete", "results": []}

    monkeypatch.setattr(mod, "_logs_client_for", lambda item: FakeLogs())
    resp = mod._logs_search(_event({"log_group": "/app/orders", "all": True}), "svc-a")
    assert resp["statusCode"] == 200
    qs = captured["queryString"]
    assert "@message like /ERROR/" not in qs
    assert "fields @timestamp, @message" in qs and "sort @timestamp desc" in qs


def test_logs_search_limit_capped_at_2000(monkeypatch):
    mod = _load()
    _fake_target_monkeypatch(mod, monkeypatch)
    captured = {}

    class FakeLogs:
        def start_query(self, **kw):
            captured.update(kw)
            return {"queryId": "q1"}
        def get_query_results(self, **kw):
            return {"status": "Complete", "results": []}

    monkeypatch.setattr(mod, "_logs_client_for", lambda item: FakeLogs())
    mod._logs_search(_event({"log_group": "/app/orders", "limit": 999999}), "svc-a")
    assert "limit 2000" in captured["queryString"]


def test_logs_search_minutes_window(monkeypatch):
    mod = _load()
    _fake_target_monkeypatch(mod, monkeypatch)
    captured = {}

    class FakeLogs:
        def start_query(self, **kw):
            captured.update(kw)
            return {"queryId": "q1"}
        def get_query_results(self, **kw):
            return {"status": "Complete", "results": []}

    monkeypatch.setattr(mod, "_logs_client_for", lambda item: FakeLogs())
    mod._logs_search(_event({"log_group": "/app/orders", "minutes": 5}), "svc-a")
    span = captured["endTime"] - captured["startTime"]
    assert span == 300
    assert captured["startTime"] < 10_000_000_000  # epoch seconds


def test_logs_search_reports_clamp(monkeypatch):
    mod = _load()
    _fake_target_monkeypatch(mod, monkeypatch)

    class FakeLogs:
        def start_query(self, **kw): return {"queryId": "q1"}
        def get_query_results(self, **kw): return {"status": "Complete", "results": []}

    monkeypatch.setattr(mod, "_logs_client_for", lambda item: FakeLogs())
    resp = mod._logs_search(_event({"log_group": "/app/orders", "start": 0, "end": 60 * 3600}), "svc-a")
    body = json.loads(resp["body"])
    assert body.get("window_clamped") is True


# ===== the log_group allowlist: added with the fix, not by the original PR =====

def _target_with(groups):
    return {"target_id": "t1", "team": "", "region": "ap-northeast-2",
            "spoke_role_arn": "", "log_groups": groups}


class _NeverCalled:
    """Any call means an unregistered log group reached CloudWatch."""

    def start_query(self, **kw):  # pragma: no cover - must never run
        raise AssertionError(f"start_query reached AWS with logGroupName={kw.get('logGroupName')!r}")


def test_an_unregistered_log_group_is_refused(monkeypatch):
    """The vulnerability this fix closes.

    `log_group` came from the request BODY and was passed straight to start_query with
    no check against the target's registered list, so any authenticated caller (a
    dbops-viewer included) could read ANY log group in the account by naming it:
    /aws/lambda/dbops-* carries the account ids, role names, ARNs and SQL fragments
    this project deliberately keeps out of API responses. `_target_visible` gates the
    TARGET, not the log group, so team scoping did not cover log content either.
    """
    mod = _load()
    monkeypatch.setattr(mod, "_get_target", lambda t: _target_with(["/app/orders"]))
    monkeypatch.setattr(mod, "_logs_client_for", lambda item: _NeverCalled())

    resp = mod._logs_search(_event({"log_group": "/aws/lambda/dbops-dev-operations-mcp"}), "t1")

    assert resp["statusCode"] == 403, resp
    body = json.loads(resp["body"])
    assert "not registered" in body["error"]
    assert body["registered_log_groups"] == ["/app/orders"]


def test_a_registered_log_group_is_still_selectable(monkeypatch):
    """Negative control: `log_groups` is a LIST an admin sets at registration, and the
    body parameter exists so a caller can pick among them. Refusing everything would
    pass the test above while deleting the feature."""
    mod = _load()
    monkeypatch.setattr(mod, "_get_target",
                        lambda t: _target_with(["/app/orders", "/app/billing"]))
    seen = {}

    class FakeLogs:
        def start_query(self, **kw):
            seen.update(kw)
            return {"queryId": "q1"}

        def get_query_results(self, **kw):
            return {"status": "Complete", "results": []}

    monkeypatch.setattr(mod, "_logs_client_for", lambda item: FakeLogs())

    resp = mod._logs_search(_event({"log_group": "/app/billing"}), "t1")

    assert resp["statusCode"] == 200, resp
    assert seen["logGroupName"] == "/app/billing"


def test_omitting_log_group_uses_the_first_registered_one(monkeypatch):
    """The pre-existing default, preserved."""
    mod = _load()
    monkeypatch.setattr(mod, "_get_target",
                        lambda t: _target_with(["/app/orders", "/app/billing"]))
    seen = {}

    class FakeLogs:
        def start_query(self, **kw):
            seen.update(kw)
            return {"queryId": "q1"}

        def get_query_results(self, **kw):
            return {"status": "Complete", "results": []}

    monkeypatch.setattr(mod, "_logs_client_for", lambda item: FakeLogs())

    resp = mod._logs_search(_event({}), "t1")
    assert resp["statusCode"] == 200
    assert seen["logGroupName"] == "/app/orders"


def test_start_query_failure_leaks_nothing_and_is_not_200(monkeypatch):
    """It returned `f"start_query failed: {e}"` at HTTP 200: raw AWS error text (hub
    account id, Lambda role name, target ARN) in the body, and a failure that looked
    like a successful empty search."""
    mod = _load()
    monkeypatch.setattr(mod, "_get_target", lambda t: _target_with(["/app/orders"]))

    class Boom:
        def start_query(self, **kw):
            raise RuntimeError(
                "User: arn:aws:sts::123456789012:assumed-role/dbops-dev-agent-ApmApiRole/x "
                "is not authorized")

    monkeypatch.setattr(mod, "_logs_client_for", lambda item: Boom())

    resp = mod._logs_search(_event({}), "t1")

    assert resp["statusCode"] == 502, resp
    body = resp["body"]
    for leak in ("arn:aws:sts", "assumed-role", "123456789012", "not authorized"):
        assert leak not in body, f"leaked {leak!r}"
