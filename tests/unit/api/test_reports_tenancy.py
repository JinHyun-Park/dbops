"""Task 2: /api/reports visibility gate.

Reports LIST: filter by visible set (no cluster_id param).
Reports GET /{id}: 403 when row's cluster_id is not visible.
Reports GET /{id}/html: 403 same gate, before presign.
Admin -> no-op (all rows / no 403).
"""

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Module loading — push api/reports on sys.path so `import tenancy` resolves
# ---------------------------------------------------------------------------

_REPORTS_DIR = Path(__file__).resolve().parents[3] / "api" / "reports"
sys.path.insert(0, str(_REPORTS_DIR))

# Stub env vars needed at module import time
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")
os.environ.setdefault("ARCHIVE_BUCKET", "dbops-test-archive")

_PATH = _REPORTS_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("reports_handler_tenancy", _PATH)
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


def _viewer_event(path="/api/reports", path_params=None, qsp=None):
    token = _make_token(groups=["dbops-viewer"], username="u-viewer")
    ev = {
        "httpMethod": "GET",
        "rawPath": path,
        "headers": {"authorization": f"Bearer {token}"},
        "requestContext": {"http": {"method": "GET", "path": path}},
        "queryStringParameters": qsp or {},
        "pathParameters": path_params or {},
    }
    return ev


def _admin_event(path="/api/reports", path_params=None, qsp=None):
    token = _make_token(groups=["dbops-admin"], username="u-admin")
    ev = {
        "httpMethod": "GET",
        "rawPath": path,
        "headers": {"authorization": f"Bearer {token}"},
        "requestContext": {"http": {"method": "GET", "path": path}},
        "queryStringParameters": qsp or {},
        "pathParameters": path_params or {},
    }
    return ev


# ---------------------------------------------------------------------------
# RDS Data API mock helpers
# ---------------------------------------------------------------------------

def _rds_list_response(rows):
    """Build a mock execute_statement response for the LIST query."""
    cols = ["id", "cluster_id", "report_type", "report_date", "summary", "created_at"]
    meta = [{"name": c, "typeName": "text"} for c in cols]
    records = []
    for r in rows:
        rec = []
        for c in cols:
            v = r.get(c, "")
            rec.append({"stringValue": str(v)} if v is not None else {"isNull": True})
        records.append(rec)
    return {"columnMetadata": meta, "records": records}


def _rds_single_response(cluster_id="c-teamB", report_id="42"):
    cols = ["id", "cluster_id", "report_type", "report_date", "summary", "created_at", "s3_key"]
    meta = [{"name": c, "typeName": "text"} for c in cols]
    vals = {
        "id": report_id,
        "cluster_id": cluster_id,
        "report_type": "daily",
        "report_date": "2026-06-24",
        "summary": "test",
        "created_at": "2026-06-24T00:00:00Z",
        "s3_key": "reports/2026/06/24/c-teamB/daily.json",
    }
    rec = [{"stringValue": str(vals[c])} for c in cols]
    return {"columnMetadata": meta, "records": [rec]}


def _rds_empty():
    return {"columnMetadata": [], "records": []}


# ---------------------------------------------------------------------------
# LIST tests
# ---------------------------------------------------------------------------

_LIST_ROWS = [
    {"id": "1", "cluster_id": "c-open",  "report_type": "daily", "report_date": "2026-06-24", "summary": "", "created_at": ""},
    {"id": "2", "cluster_id": "c-teamA", "report_type": "daily", "report_date": "2026-06-24", "summary": "", "created_at": ""},
    {"id": "3", "cluster_id": "c-teamB", "report_type": "daily", "report_date": "2026-06-24", "summary": "", "created_at": ""},
]


def test_reports_list_viewer_excludes_other_team(monkeypatch):
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
    rows = json.loads(r["body"])
    ids = {row["cluster_id"] for row in rows}
    assert "c-teamB" not in ids
    assert "c-open" in ids
    assert "c-teamA" in ids


def test_reports_list_admin_sees_all(monkeypatch):
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
    rows = json.loads(r["body"])
    ids = {row["cluster_id"] for row in rows}
    assert ids == {"c-open", "c-teamA", "c-teamB"}


# ---------------------------------------------------------------------------
# GET /{id} tests
# ---------------------------------------------------------------------------

def test_reports_get_by_id_forbidden_for_viewer(monkeypatch):
    """Viewer reads report whose cluster_id is c-teamB; cluster_visible=False => 403."""
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = _rds_single_response("c-teamB", "42")
    monkeypatch.setattr(handler, "boto3", MagicMock(**{"client.return_value": mock_rds}))
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: False)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tB"})

    r = handler.lambda_handler(
        _viewer_event("/api/reports/42", {"id": "42"}), None
    )
    assert r["statusCode"] == 403
    body = json.loads(r["body"])
    assert "접근 권한" in body.get("error", "") or "접근 권한" in body.get("reason", "")


def test_reports_get_by_id_allowed_when_visible(monkeypatch):
    """cluster_visible=True => 200 (not 403)."""
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = _rds_single_response("c-teamA", "42")
    monkeypatch.setattr(handler, "boto3", MagicMock(**{"client.return_value": mock_rds}))
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tA"})

    r = handler.lambda_handler(
        _viewer_event("/api/reports/42", {"id": "42"}), None
    )
    assert r["statusCode"] != 403


# ---------------------------------------------------------------------------
# GET /{id}/html tests
# ---------------------------------------------------------------------------

def test_reports_html_forbidden_for_viewer(monkeypatch):
    """HTML arm: row's cluster_id not visible => 403 before presign."""
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = _rds_single_response("c-teamB", "42")
    mock_s3 = MagicMock()
    monkeypatch.setattr(
        handler, "boto3",
        MagicMock(**{
            "client.side_effect": lambda svc, **kw: mock_rds if svc == "rds-data" else mock_s3,
        }),
    )
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: False)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tB"})

    r = handler.lambda_handler(
        _viewer_event("/api/reports/42/html", {"id": "42"}), None
    )
    assert r["statusCode"] == 403
    # presign must NOT have been called
    mock_s3.generate_presigned_url.assert_not_called()


def test_reports_html_allowed_when_visible(monkeypatch):
    """HTML arm: cluster_visible=True => proceeds (not 403); presign called."""
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = _rds_single_response("c-teamA", "42")
    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {}
    mock_s3.generate_presigned_url.return_value = "https://presigned.example.com/report.html"
    monkeypatch.setattr(
        handler, "boto3",
        MagicMock(**{
            "client.side_effect": lambda svc, **kw: mock_rds if svc == "rds-data" else mock_s3,
        }),
    )
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, item: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tA"})

    r = handler.lambda_handler(
        _viewer_event("/api/reports/42/html", {"id": "42"}), None
    )
    assert r["statusCode"] != 403
    mock_s3.generate_presigned_url.assert_called_once()
