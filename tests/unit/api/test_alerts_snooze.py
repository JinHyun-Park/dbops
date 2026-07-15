"""P2-⑦: alarm snooze + inline threshold edit.

Covers:
  - _snooze_rule: minutes>0 sets snooze_until, minutes<=0 clears it, invalid
    id / not-found handled
  - _snooze_bulk: updates every rule for a cluster_id, invalid cluster_id,
    tenant-scoping 403 when the cluster isn't visible to the caller
  - _update_rule: comparison is now an updatable field alongside threshold
  - _list_rules: SELECT now includes snooze_until so the UI can render it
  - admin gate (_forbid_viewer) on both new POST routes via lambda_handler
"""

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[3]
_ALERTS_DIR = ROOT / "api" / "alerts"
sys.path.insert(0, str(_ALERTS_DIR))

os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")
os.environ.setdefault("ALERT_TOPIC_ARN", "")
os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")

_HANDLER_PATH = _ALERTS_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("alerts_handler_snooze", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
sys.modules["alerts_handler_snooze"] = handler
_spec.loader.exec_module(handler)


# ---------------------------------------------------------------------------
# _snooze_rule
# ---------------------------------------------------------------------------

def test_snooze_rule_sets_snooze_until():
    seen = {}

    def query(sql, params=None):
        seen["sql"] = sql
        seen["params"] = params
        return [{"id": 5, "cluster_id": "c1", "name": "r", "snooze_until": "2026-07-15T01:00:00Z"}]

    resp = handler._snooze_rule(query, "5", {"minutes": 30})
    assert resp["statusCode"] == 200
    assert "snooze_until = NOW() + (:mins || ' minutes')::interval" in seen["sql"]
    assert seen["params"] == {"id": 5, "mins": "30"}
    body = json.loads(resp["body"])
    assert body["rule"]["id"] == 5


def test_snooze_rule_zero_minutes_clears():
    seen = {}

    def query(sql, params=None):
        seen["sql"] = sql
        seen["params"] = params
        return [{"id": 5, "cluster_id": "c1", "name": "r", "snooze_until": None}]

    resp = handler._snooze_rule(query, "5", {"minutes": 0})
    assert resp["statusCode"] == 200
    assert "snooze_until = NULL" in seen["sql"]
    assert seen["params"] == {"id": 5}
    body = json.loads(resp["body"])
    assert body["rule"]["snooze_until"] is None


def test_snooze_rule_missing_minutes_defaults_to_clear():
    """No `minutes` in body → treated as 0 → clears rather than erroring."""
    seen = {}

    def query(sql, params=None):
        seen["sql"] = sql
        return [{"id": 5, "cluster_id": "c1", "name": "r", "snooze_until": None}]

    resp = handler._snooze_rule(query, "5", {})
    assert resp["statusCode"] == 200
    assert "snooze_until = NULL" in seen["sql"]


def test_snooze_rule_invalid_id_returns_400():
    resp = handler._snooze_rule(MagicMock(), "not-an-id", {"minutes": 30})
    assert resp["statusCode"] == 400


def test_snooze_rule_not_found_returns_404():
    resp = handler._snooze_rule(lambda sql, params=None: [], "999", {"minutes": 30})
    assert resp["statusCode"] == 404


# ---------------------------------------------------------------------------
# _snooze_bulk
# ---------------------------------------------------------------------------

def test_snooze_bulk_updates_all_rules_for_cluster(monkeypatch):
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: True)
    seen = {}

    def query(sql, params=None):
        seen["sql"] = sql
        seen["params"] = params
        return [{"id": 1}, {"id": 2}, {"id": 3}]

    resp = handler._snooze_bulk({}, query, {"cluster_id": "c-teamA", "minutes": 60})
    assert resp["statusCode"] == 200
    assert seen["params"] == {"cid": "c-teamA", "mins": "60"}
    assert "WHERE cluster_id = :cid" in seen["sql"]
    body = json.loads(resp["body"])
    assert body["cluster_id"] == "c-teamA"
    assert body["updated"] == 3


def test_snooze_bulk_zero_minutes_clears_cluster(monkeypatch):
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: True)
    seen = {}

    def query(sql, params=None):
        seen["sql"] = sql
        return [{"id": 1}]

    resp = handler._snooze_bulk({}, query, {"cluster_id": "c-teamA", "minutes": 0})
    assert resp["statusCode"] == 200
    assert "snooze_until = NULL" in seen["sql"]


def test_snooze_bulk_invalid_cluster_id_returns_400(monkeypatch):
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: True)
    resp = handler._snooze_bulk({}, MagicMock(), {"cluster_id": "bad id!", "minutes": 60})
    assert resp["statusCode"] == 400


def test_snooze_bulk_forbidden_when_cluster_not_visible(monkeypatch):
    """Tenant scoping: caller isn't a member of the cluster's team → 403,
    mirroring the /impact endpoint's cluster_visible check."""
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: False)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tB"})
    query = MagicMock()
    resp = handler._snooze_bulk({}, query, {"cluster_id": "c-teamB", "minutes": 60})
    assert resp["statusCode"] == 403
    query.assert_not_called()


# ---------------------------------------------------------------------------
# _update_rule — comparison is now updatable alongside threshold
# ---------------------------------------------------------------------------

def test_update_rule_comparison_field():
    seen = {}

    def query(sql, params=None):
        seen["sql"] = sql
        seen["params"] = params
        return [{"id": 1, "cluster_id": "c1", "name": "r", "metric_type": "cpu",
                 "comparison": "<", "threshold": 80.0, "enabled": True, "snooze_until": None}]

    resp = handler._update_rule(query, "1", {"comparison": "<"})
    assert resp["statusCode"] == 200
    assert "comparison = :comparison" in seen["sql"]
    assert seen["params"]["comparison"] == "<"


def test_update_rule_invalid_comparison_returns_400():
    resp = handler._update_rule(MagicMock(), "1", {"comparison": "roughly"})
    assert resp["statusCode"] == 400


def test_update_rule_threshold_still_works():
    seen = {}

    def query(sql, params=None):
        seen["params"] = params
        return [{"id": 1, "cluster_id": "c1", "name": "r", "metric_type": "cpu",
                 "comparison": ">", "threshold": 90.0, "enabled": True, "snooze_until": None}]

    resp = handler._update_rule(query, "1", {"threshold": 90})
    assert resp["statusCode"] == 200
    assert seen["params"]["threshold"] == 90.0


# ---------------------------------------------------------------------------
# _list_rules — snooze_until now selected
# ---------------------------------------------------------------------------

def test_list_rules_selects_snooze_until():
    seen = {}

    def query(sql, params=None):
        seen["sql"] = sql
        return []

    handler._list_rules(query, None)
    assert "r.snooze_until" in seen["sql"]


# ---------------------------------------------------------------------------
# Admin gate on the new routes (lambda_handler dispatch)
# ---------------------------------------------------------------------------

def _make_token(groups=None, username="u-test"):
    claims = {"cognito:username": username}
    if groups is not None:
        claims["cognito:groups"] = groups
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def _event(groups, path, path_params=None, body=None):
    token = _make_token(groups=groups)
    return {
        "httpMethod": "POST",
        "rawPath": path,
        "headers": {"authorization": f"Bearer {token}"},
        "requestContext": {"http": {"method": "POST", "path": path}},
        "pathParameters": path_params or {},
        "queryStringParameters": {},
        "body": json.dumps(body or {}),
    }


def test_snooze_route_forbidden_for_viewer(monkeypatch):
    monkeypatch.setattr(handler, "boto3", MagicMock(**{"client.return_value": MagicMock()}))
    r = handler.lambda_handler(
        _event(["dbops-viewer"], "/api/alert-rules/1/snooze", {"id": "1"}, {"minutes": 30}),
        None,
    )
    assert r["statusCode"] == 403


def test_snooze_bulk_route_forbidden_for_viewer(monkeypatch):
    monkeypatch.setattr(handler, "boto3", MagicMock(**{"client.return_value": MagicMock()}))
    r = handler.lambda_handler(
        _event(["dbops-viewer"], "/api/alert-rules/snooze-bulk", {}, {"cluster_id": "c1", "minutes": 30}),
        None,
    )
    assert r["statusCode"] == 403


def test_snooze_route_admin_reaches_handler(monkeypatch):
    """Admin passes the gate and _snooze_rule actually runs against the mocked DB."""
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = {
        "columnMetadata": [{"name": c} for c in ("id", "cluster_id", "name", "snooze_until")],
        "records": [[{"longValue": 1}, {"stringValue": "c1"}, {"stringValue": "r"}, {"stringValue": "2026-07-15T01:00:00"}]],
    }
    monkeypatch.setattr(handler, "boto3", MagicMock(**{"client.return_value": mock_rds}))
    r = handler.lambda_handler(
        _event(["dbops-admin"], "/api/alert-rules/1/snooze", {"id": "1"}, {"minutes": 30}),
        None,
    )
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["rule"]["id"] == 1
