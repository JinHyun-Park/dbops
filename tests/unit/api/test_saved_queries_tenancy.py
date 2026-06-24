"""Task 2: /api/saved-queries visibility gate.

saved_queries LIST: filter by visible set (no cluster_id param).
saved_queries GET /{id}: 403 when row's cluster_id is not visible.
Admin -> no-op (all rows / no 403).
Writes (POST/PUT/DELETE) are admin-gated -> SKIP (admins see all).
"""

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Module loading — push api/saved_queries on sys.path so `import tenancy` resolves
# ---------------------------------------------------------------------------

_SQ_DIR = Path(__file__).resolve().parents[3] / "api" / "saved_queries"
sys.path.insert(0, str(_SQ_DIR))

# Stub env vars needed at module import time
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

_PATH = _SQ_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("saved_queries_handler_tenancy", _PATH)
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


def _viewer_event(method="GET", path="/api/saved-queries", path_params=None, qsp=None, body=None):
    token = _make_token(groups=["dbops-viewer"], username="u-viewer")
    ev = {
        "httpMethod": method,
        "rawPath": path,
        "headers": {"authorization": f"Bearer {token}"},
        "requestContext": {"http": {"method": method, "path": path}},
        "queryStringParameters": qsp or {},
        "pathParameters": path_params or {},
    }
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


def _admin_event(method="GET", path="/api/saved-queries", path_params=None, qsp=None, body=None):
    token = _make_token(groups=["dbops-admin"], username="u-admin")
    ev = {
        "httpMethod": method,
        "rawPath": path,
        "headers": {"authorization": f"Bearer {token}"},
        "requestContext": {"http": {"method": method, "path": path}},
        "queryStringParameters": qsp or {},
        "pathParameters": path_params or {},
    }
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


# ---------------------------------------------------------------------------
# RDS Data API mock helpers
# ---------------------------------------------------------------------------

def _rds_list_response(rows):
    cols = ["id", "cluster_id", "title", "description", "tags", "created_by", "created_at", "updated_at"]
    meta = [{"name": c, "typeName": "text"} for c in cols]
    records = []
    for r in rows:
        rec = []
        for c in cols:
            v = r.get(c, "")
            rec.append({"stringValue": str(v)} if v is not None else {"isNull": True})
        records.append(rec)
    return {"columnMetadata": meta, "records": records}


def _rds_single_response(cluster_id="c-teamB", sq_id="7"):
    cols = ["id", "cluster_id", "title", "description", "sql_text", "tags", "created_by", "created_at", "updated_at"]
    meta = [{"name": c, "typeName": "text"} for c in cols]
    vals = {
        "id": sq_id,
        "cluster_id": cluster_id,
        "title": "test query",
        "description": "",
        "sql_text": "SELECT 1",
        "tags": "",
        "created_by": "u-admin",
        "created_at": "2026-06-24T00:00:00Z",
        "updated_at": "2026-06-24T00:00:00Z",
    }
    rec = [{"stringValue": str(vals[c])} for c in cols]
    return {"columnMetadata": meta, "records": [rec]}


# ---------------------------------------------------------------------------
# LIST tests
# ---------------------------------------------------------------------------

_LIST_ROWS = [
    {"id": "1", "cluster_id": "c-open",  "title": "q1", "description": "", "tags": "", "created_by": "", "created_at": "", "updated_at": ""},
    {"id": "2", "cluster_id": "c-teamA", "title": "q2", "description": "", "tags": "", "created_by": "", "created_at": "", "updated_at": ""},
    {"id": "3", "cluster_id": "c-teamB", "title": "q3", "description": "", "tags": "", "created_by": "", "created_at": "", "updated_at": ""},
]


def test_saved_queries_list_viewer_excludes_other_team(monkeypatch):
    """Viewer: rows for c-open/c-teamA/c-teamB; visible_set = {c-open, c-teamA}
    => c-teamB excluded."""
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = _rds_list_response(_LIST_ROWS)
    monkeypatch.setattr(handler, "boto3", MagicMock(**{"client.return_value": mock_rds}))
    monkeypatch.setattr(
        handler.tenancy, "visible_set_from_registry",
        lambda ev: {"c-open", "c-teamA"},
    )

    r = handler.lambda_handler(_viewer_event(), None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    ids = {row["cluster_id"] for row in body.get("queries", [])}
    assert "c-teamB" not in ids
    assert "c-open" in ids
    assert "c-teamA" in ids


def test_saved_queries_list_admin_sees_all(monkeypatch):
    """Admin: visible_set_from_registry returns None => all rows pass through."""
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = _rds_list_response(_LIST_ROWS)
    monkeypatch.setattr(handler, "boto3", MagicMock(**{"client.return_value": mock_rds}))
    monkeypatch.setattr(
        handler.tenancy, "visible_set_from_registry",
        lambda ev: None,
    )

    r = handler.lambda_handler(_admin_event(), None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    ids = {row["cluster_id"] for row in body.get("queries", [])}
    assert ids == {"c-open", "c-teamA", "c-teamB"}


# ---------------------------------------------------------------------------
# GET /{id} tests
# ---------------------------------------------------------------------------

def test_saved_queries_get_by_id_forbidden_for_viewer(monkeypatch):
    """Viewer reads query whose cluster_id is c-teamB; cluster_visible=False => 403."""
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = _rds_single_response("c-teamB", "7")
    monkeypatch.setattr(handler, "boto3", MagicMock(**{"client.return_value": mock_rds}))
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: False)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tB"})

    r = handler.lambda_handler(
        _viewer_event("GET", "/api/saved-queries/7", {"id": "7"}), None
    )
    assert r["statusCode"] == 403
    body = json.loads(r["body"])
    assert "접근 권한" in body.get("error", "") or "접근 권한" in body.get("reason", "")


def test_saved_queries_get_by_id_allowed_when_visible(monkeypatch):
    """cluster_visible=True => 200 (not 403)."""
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = _rds_single_response("c-teamA", "7")
    monkeypatch.setattr(handler, "boto3", MagicMock(**{"client.return_value": mock_rds}))
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tA"})

    r = handler.lambda_handler(
        _viewer_event("GET", "/api/saved-queries/7", {"id": "7"}), None
    )
    assert r["statusCode"] != 403
