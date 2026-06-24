"""Task 5: /api/dashboard/* per-cluster visibility gate + fleet filter.

Per-cluster gate:
  - viewer on other-team cluster => 403
  - viewer on unassigned cluster => NOT 403
  - admin on other-team cluster  => NOT 403

Fleet filter (_multi_cluster_overview):
  - viewer in team A => sees c-open + c-teamA, NOT c-teamB
  - admin            => sees all 3
"""

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Module loading — push api/dashboard on sys.path so engine_family + tenancy resolve
# ---------------------------------------------------------------------------

_DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "api" / "dashboard"
sys.path.insert(0, str(_DASHBOARD_DIR))

_PATH = _DASHBOARD_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("dashboard_handler_tenancy", _PATH)

# Stub env vars needed at module import time
os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dash)

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


def _admin_event(method="GET", path="/api/dashboard/c1/overview", path_params=None):
    token = _make_token(groups=["dbops-admin"], username="u-admin")
    ev = {
        "httpMethod": method,
        "rawPath": path,
        "path": path,
        "headers": {"authorization": f"Bearer {token}"},
        "requestContext": {"http": {"method": method, "path": path}},
        "queryStringParameters": {},
    }
    if path_params:
        ev["pathParameters"] = path_params
    return ev


def _viewer_event(method="GET", path="/api/dashboard/c1/overview", path_params=None, username="u-viewer"):
    token = _make_token(groups=["dbops-viewer"], username=username)
    ev = {
        "httpMethod": method,
        "rawPath": path,
        "path": path,
        "headers": {"authorization": f"Bearer {token}"},
        "requestContext": {"http": {"method": method, "path": path}},
        "queryStringParameters": {},
    }
    if path_params:
        ev["pathParameters"] = path_params
    return ev


# ---------------------------------------------------------------------------
# Per-cluster gate tests
# ---------------------------------------------------------------------------

def test_dashboard_viewer_blocked_on_other_team_cluster(monkeypatch):
    """Viewer on a cluster assigned to a different team => 403."""
    monkeypatch.setattr(dash, "_lookup_cluster", lambda cid: {"cluster_id": cid, "team_id": "tB"})
    monkeypatch.setattr(dash.tenancy, "my_team_ids", lambda u: {"tA"})

    r = dash.lambda_handler(
        _viewer_event("GET", "/api/dashboard/c1/overview", {"cluster_id": "c1"}),
        None,
    )
    assert r["statusCode"] == 403
    body = json.loads(r["body"])
    assert body["error"] == "forbidden"
    assert "접근 권한" in body["reason"]


def test_dashboard_viewer_allowed_on_unassigned(monkeypatch):
    """Viewer on an unassigned cluster (no team_id) => NOT 403."""
    monkeypatch.setattr(dash, "_lookup_cluster", lambda cid: {"cluster_id": cid})
    monkeypatch.setattr(dash.tenancy, "my_team_ids", lambda u: set())
    # Stub the RDS Data client so the handler doesn't error on network calls
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = {
        "columnMetadata": [{"name": "metric_type"}, {"name": "value"}, {"name": "ts"}],
        "records": [],
    }
    monkeypatch.setattr(dash, "_rds_data", lambda: mock_rds)

    r = dash.lambda_handler(
        _viewer_event("GET", "/api/dashboard/c1/timeseries", {"cluster_id": "c1"}),
        None,
    )
    assert r["statusCode"] != 403


def test_dashboard_admin_always_allowed(monkeypatch):
    """Admin on a cluster assigned to another team => NOT 403."""
    monkeypatch.setattr(dash, "_lookup_cluster", lambda cid: {"cluster_id": cid, "team_id": "tB"})
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = {
        "columnMetadata": [{"name": "metric_type"}, {"name": "value"}, {"name": "ts"}],
        "records": [],
    }
    monkeypatch.setattr(dash, "_rds_data", lambda: mock_rds)

    r = dash.lambda_handler(
        _admin_event("GET", "/api/dashboard/c1/timeseries", {"cluster_id": "c1"}),
        None,
    )
    assert r["statusCode"] != 403


# ---------------------------------------------------------------------------
# Fleet filter tests
# ---------------------------------------------------------------------------

_REGISTERED = {
    "c-open":  {"engine": "aurora-postgresql", "team_id": ""},
    "c-teamA": {"engine": "aurora-postgresql", "team_id": "tA"},
    "c-teamB": {"engine": "aurora-postgresql", "team_id": "tB"},
}

_FLEET_ROWS = [
    {"cluster_id": "c-open",  "engine": "aurora-postgresql", "engine_version": None,
     "status": "available", "storage_size_gb": None, "cpu": None, "aas": None,
     "conn_active": None, "conn_idle": None, "storage_bytes": None,
     "deadlocks": None, "blocking_count": 0},
    {"cluster_id": "c-teamA", "engine": "aurora-postgresql", "engine_version": None,
     "status": "available", "storage_size_gb": None, "cpu": None, "aas": None,
     "conn_active": None, "conn_idle": None, "storage_bytes": None,
     "deadlocks": None, "blocking_count": 0},
    {"cluster_id": "c-teamB", "engine": "aurora-postgresql", "engine_version": None,
     "status": "available", "storage_size_gb": None, "cpu": None, "aas": None,
     "conn_active": None, "conn_idle": None, "storage_bytes": None,
     "deadlocks": None, "blocking_count": 0},
]


def _make_fleet_query(rows):
    def _q(sql, params=None):
        return list(rows)
    return _q


def test_fleet_viewer_in_team_a_sees_open_and_team_a(monkeypatch):
    """Viewer in team A: c-open + c-teamA visible; c-teamB hidden."""
    monkeypatch.setattr(dash, "_registered_clusters", lambda: dict(_REGISTERED))
    monkeypatch.setattr(dash.tenancy, "my_team_ids", lambda u: {"tA"})

    result = dash._multi_cluster_overview(_make_fleet_query(_FLEET_ROWS), _viewer_event())
    ids = {c["cluster_id"] for c in result["clusters"]}
    assert "c-open" in ids
    assert "c-teamA" in ids
    assert "c-teamB" not in ids


def test_fleet_admin_sees_all(monkeypatch):
    """Admin: all 3 clusters visible (visible_cluster_ids returns None)."""
    monkeypatch.setattr(dash, "_registered_clusters", lambda: dict(_REGISTERED))

    result = dash._multi_cluster_overview(_make_fleet_query(_FLEET_ROWS), _admin_event())
    ids = {c["cluster_id"] for c in result["clusters"]}
    assert ids == {"c-open", "c-teamA", "c-teamB"}


def test_fleet_registry_failure_leaves_rows_unfiltered(monkeypatch):
    """If _registered_clusters returns None (DDB outage), rows stay unfiltered."""
    monkeypatch.setattr(dash, "_registered_clusters", lambda: None)

    result = dash._multi_cluster_overview(_make_fleet_query(_FLEET_ROWS), _viewer_event())
    ids = {c["cluster_id"] for c in result["clusters"]}
    # All rows pass through unfiltered
    assert ids == {"c-open", "c-teamA", "c-teamB"}


def test_registered_clusters_projection_includes_team_id():
    """Source-level guard (whole-branch review M1): the fleet filter is only
    correct if _registered_clusters projects team_id. The mock-based fleet
    tests above can't catch a projection regression (they mock the function
    away), so assert the real ProjectionExpression carries team_id — dropping
    it would silently turn the fleet filter into a no-op (cross-team leak)."""
    src = _PATH.read_text()
    proj_lines = [ln for ln in src.splitlines() if "ProjectionExpression" in ln]
    assert proj_lines, "no ProjectionExpression found in dashboard handler"
    # The registry projection (the one that selects cluster_id) MUST carry
    # team_id; targeting the cluster_id signature avoids false failures if an
    # unrelated projection is added later.
    registry_projs = [ln for ln in proj_lines if "cluster_id" in ln]
    assert registry_projs, "no cluster_id ProjectionExpression found (registry scan)"
    assert all("team_id" in ln for ln in registry_projs), (
        "the _registered_clusters ProjectionExpression dropped team_id — the fleet "
        "visibility filter would silently leak other teams' clusters"
    )
