"""Task 3: /api/alerts visibility gate.

Alerts LIST (GET /api/alerts): filter rules by visible set.
Alerts GET /{id}/impact: 403 when rule's cluster_id is not visible.
Admin -> no-op (all rows / no 403).
POST/DELETE are _forbid_viewer-gated at handler level -> SKIP.
"""

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Module loading — push api/alerts on sys.path so `import tenancy` resolves
# ---------------------------------------------------------------------------

_ALERTS_DIR = Path(__file__).resolve().parents[3] / "api" / "alerts"
sys.path.insert(0, str(_ALERTS_DIR))

# Stub env vars needed at module import time
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")
os.environ.setdefault("ALERT_TOPIC_ARN", "")
os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")

_PATH = _ALERTS_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("alerts_handler_tenancy", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")
    monkeypatch.setenv("TEAM_MEMBERS_TABLE", "team-members-stub")
    monkeypatch.setenv("TEAM_MEMBERS_BY_USER_INDEX", "by-user")


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------

def _make_token(groups=None, username="u-test"):
    claims = {"cognito:username": username}
    if groups is not None:
        claims["cognito:groups"] = groups
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def _viewer_event(path="/api/alerts", method="GET", path_params=None, qsp=None):
    token = _make_token(groups=["dbops-viewer"], username="u-viewer")
    return {
        "httpMethod": method,
        "rawPath": path,
        "headers": {"authorization": f"Bearer {token}"},
        "requestContext": {"http": {"method": method, "path": path}},
        "queryStringParameters": qsp or {},
        "pathParameters": path_params or {},
    }


def _admin_event(path="/api/alerts", method="GET", path_params=None, qsp=None):
    token = _make_token(groups=["dbops-admin"], username="u-admin")
    return {
        "httpMethod": method,
        "rawPath": path,
        "headers": {"authorization": f"Bearer {token}"},
        "requestContext": {"http": {"method": method, "path": path}},
        "queryStringParameters": qsp or {},
        "pathParameters": path_params or {},
    }


# ---------------------------------------------------------------------------
# RDS Data API mock helpers
# ---------------------------------------------------------------------------

_LIST_RULES = [
    {"id": 1, "cluster_id": "c-open",  "name": "cpu-open",  "metric_type": "cpu_utilization",
     "comparison": ">", "threshold": 80.0, "enabled": True, "last_triggered_at": None,
     "last_acked_at": None, "last_acked_by": None, "created_at": "2026-01-01T00:00:00Z",
     "conditions_json": None, "latest_metric_ts": None, "data_status": "no_data"},
    {"id": 2, "cluster_id": "c-teamA", "name": "cpu-teamA", "metric_type": "cpu_utilization",
     "comparison": ">", "threshold": 80.0, "enabled": True, "last_triggered_at": None,
     "last_acked_at": None, "last_acked_by": None, "created_at": "2026-01-01T00:00:00Z",
     "conditions_json": None, "latest_metric_ts": None, "data_status": "no_data"},
    {"id": 3, "cluster_id": "c-teamB", "name": "cpu-teamB", "metric_type": "cpu_utilization",
     "comparison": ">", "threshold": 80.0, "enabled": True, "last_triggered_at": None,
     "last_acked_at": None, "last_acked_by": None, "created_at": "2026-01-01T00:00:00Z",
     "conditions_json": None, "latest_metric_ts": None, "data_status": "no_data"},
]


def _rds_rules_response(rules):
    cols = ["id", "cluster_id", "name", "metric_type", "comparison", "threshold",
            "enabled", "last_triggered_at", "last_acked_at", "last_acked_by",
            "created_at", "conditions_json", "latest_metric_ts", "data_status"]
    meta = [{"name": c, "typeName": "text"} for c in cols]
    records = []
    for r in rules:
        rec = []
        for c in cols:
            v = r.get(c)
            if v is None:
                rec.append({"isNull": True})
            elif isinstance(v, bool):
                rec.append({"booleanValue": v})
            elif isinstance(v, int):
                rec.append({"longValue": v})
            elif isinstance(v, float):
                rec.append({"doubleValue": v})
            else:
                rec.append({"stringValue": str(v)})
        records.append(rec)
    return {"columnMetadata": meta, "records": records}


def _rds_rule_single(cluster_id="c-teamB", rule_id=3, triggered_at=None):
    cols = ["id", "cluster_id", "name", "metric_type", "comparison", "threshold",
            "last_triggered_at"]
    meta = [{"name": c, "typeName": "text"} for c in cols]
    vals = {
        "id": rule_id,
        "cluster_id": cluster_id,
        "name": "cpu-rule",
        "metric_type": "cpu_utilization",
        "comparison": ">",
        "threshold": 80.0,
        "last_triggered_at": triggered_at,
    }
    rec = []
    for c in cols:
        v = vals[c]
        if v is None:
            rec.append({"isNull": True})
        elif isinstance(v, int):
            rec.append({"longValue": v})
        elif isinstance(v, float):
            rec.append({"doubleValue": v})
        else:
            rec.append({"stringValue": str(v)})
    return {"columnMetadata": meta, "records": [rec]}


def _rds_empty():
    return {"columnMetadata": [], "records": []}


# ---------------------------------------------------------------------------
# LIST (GET /api/alerts) tests
# ---------------------------------------------------------------------------

def test_alerts_list_viewer_excludes_other_team(monkeypatch):
    """Viewer: rules for c-open/c-teamA/c-teamB; visible_set = {c-open, c-teamA}
    => c-teamB rule excluded."""
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = _rds_rules_response(_LIST_RULES)
    monkeypatch.setattr(handler, "boto3", MagicMock(**{"client.return_value": mock_rds}))
    monkeypatch.setattr(
        handler.tenancy, "visible_set_from_registry",
        lambda ev: {"c-open", "c-teamA"},
    )

    r = handler.lambda_handler(_viewer_event(), None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    rules = body.get("rules", [])
    cluster_ids = {rule["cluster_id"] for rule in rules}
    assert "c-teamB" not in cluster_ids
    assert "c-open" in cluster_ids
    assert "c-teamA" in cluster_ids


def test_alerts_list_admin_sees_all(monkeypatch):
    """Admin: visible_set_from_registry returns None => all rules pass through."""
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = _rds_rules_response(_LIST_RULES)
    monkeypatch.setattr(handler, "boto3", MagicMock(**{"client.return_value": mock_rds}))
    monkeypatch.setattr(
        handler.tenancy, "visible_set_from_registry",
        lambda ev: None,
    )

    r = handler.lambda_handler(_admin_event(), None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    rules = body.get("rules", [])
    cluster_ids = {rule["cluster_id"] for rule in rules}
    assert cluster_ids == {"c-open", "c-teamA", "c-teamB"}


# ---------------------------------------------------------------------------
# GET /{id}/impact tests
# ---------------------------------------------------------------------------

def test_alerts_impact_forbidden_for_viewer(monkeypatch):
    """Viewer reads impact for rule whose cluster_id is c-teamB; cluster_visible=False => 403."""
    mock_rds = MagicMock()
    # First call: fetch the rule
    mock_rds.execute_statement.return_value = _rds_rule_single("c-teamB", 3)
    monkeypatch.setattr(handler, "boto3", MagicMock(**{"client.return_value": mock_rds}))
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: False)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tB"})

    r = handler.lambda_handler(
        _viewer_event("/api/alerts/3/impact", path_params={"id": "3"}), None
    )
    assert r["statusCode"] == 403
    body = json.loads(r["body"])
    assert "접근 권한" in body.get("error", "")


def test_alerts_impact_allowed_when_visible(monkeypatch):
    """cluster_visible=True => proceeds (not 403)."""
    mock_rds = MagicMock()
    # First call: fetch the rule; subsequent calls return empty for the sub-queries
    mock_rds.execute_statement.side_effect = [
        _rds_rule_single("c-teamA", 2),
        _rds_empty(),
        _rds_empty(),
        _rds_empty(),
    ]
    monkeypatch.setattr(handler, "boto3", MagicMock(**{"client.return_value": mock_rds}))
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tA"})

    r = handler.lambda_handler(
        _viewer_event("/api/alerts/2/impact", path_params={"id": "2"}), None
    )
    assert r["statusCode"] != 403
