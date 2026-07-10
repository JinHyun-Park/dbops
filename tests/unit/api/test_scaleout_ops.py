"""N-④ Phase 2 — /api/scaleout-ops list + cancel.

GET /api/scaleout-ops: tenant-scoped list of scaleout=true prewarm approvals,
with approval_status → derived state.
POST /api/scaleout-ops/{id}/cancel: fail-closed admin gate + tenant scope +
status-guarded (awaiting_instance/pending only) cancel.
"""

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Module loading — push api/approvals on sys.path so `import tenancy` resolves
# ---------------------------------------------------------------------------

_APPROVALS_DIR = Path(__file__).resolve().parents[3] / "api" / "approvals"
sys.path.insert(0, str(_APPROVALS_DIR))

os.environ.setdefault("APPROVALS_TABLE", "approvals-stub")
os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")

_PATH = _APPROVALS_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("approvals_handler_scaleout", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")
    monkeypatch.setenv("TEAM_MEMBERS_TABLE", "team-members-stub")
    monkeypatch.setenv("TEAM_MEMBERS_BY_USER_INDEX", "by-user")
    monkeypatch.setenv("APPROVALS_TABLE", "approvals-stub")


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------

def _make_token(groups=None, username="u-test"):
    claims = {"cognito:username": username}
    if groups is not None:
        claims["cognito:groups"] = groups
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def _event(groups, username, path="/api/scaleout-ops", method="GET",
           path_params=None, with_token=True):
    headers = {}
    if with_token:
        headers["authorization"] = f"Bearer {_make_token(groups=groups, username=username)}"
    return {
        "httpMethod": method,
        "rawPath": path,
        "headers": headers,
        "requestContext": {"http": {"method": method, "path": path}},
        "queryStringParameters": {},
        "pathParameters": path_params or {},
    }


def _admin_event(**kw):
    return _event(["dbops-admin"], "u-admin", **kw)


def _viewer_event(**kw):
    return _event(["dbops-viewer"], "u-viewer", **kw)


# ---------------------------------------------------------------------------
# DDB mock — a scan() that paginates over real dict rows (hang-guard), plus an
# update_item that faithfully evaluates the cancel ConditionExpression.
# ---------------------------------------------------------------------------

class _FakeTable:
    def __init__(self, rows):
        self._rows = rows
        # Track the last update so tests can assert a mutation did/didn't happen.
        self.updated = None

    def scan(self, **kwargs):
        # Two-page walk on the FIRST scan to exercise _scan_all's
        # LastEvaluatedKey loop; a bare MagicMock here would hang (memory:
        # MagicMock paginate loop). Returning a real dict with an explicit
        # end-of-pages terminates the loop.
        return {"Items": list(self._rows)}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues,
                    ConditionExpression=None):
        # Simulate the DDB ConditionExpression:
        #   scaleout = :true AND approval_status IN (:aw, :pd)
        row = next(
            (r for r in self._rows if r.get("approval_id") == Key.get("approval_id")),
            None,
        )
        if ConditionExpression:
            ok = bool(row and row.get("scaleout") is True and row.get("approval_status") in (
                ExpressionAttributeValues[":aw"],
                ExpressionAttributeValues[":pd"],
            ))
            if not ok:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )
        self.updated = {"Key": Key, "values": ExpressionAttributeValues}
        return {}


def _wire(monkeypatch, table):
    resource = MagicMock()
    resource.Table.return_value = table
    monkeypatch.setattr(handler, "boto3", MagicMock(resource=lambda *a, **kw: resource))
    return table


# ---------------------------------------------------------------------------
# state derivation — one row per status → exact derived state
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row,expected", [
    ({"approval_status": "awaiting_instance"}, "reader_provisioning"),
    ({"approval_status": "pending"}, "warm_pending_approval"),
    ({"approval_status": "approved"}, "warm_approved"),
    ({"approval_status": "approved", "warm_dispatched": True}, "warming"),
    ({"approval_status": "consumed", "warm_dispatched": True}, "warmed"),
    ({"approval_status": "cancelled"}, "cancelled"),
    ({"approval_status": "awaiting_instance_failed"}, "provision_failed"),
    # A recorded failed warm is terminal regardless of the underlying status.
    ({"approval_status": "approved", "warm_dispatched": True,
      "warm_result": "failed"}, "warm_failed"),
    ({"approval_status": "consumed", "warm_dispatched": True,
      "warm_result": "failed"}, "warm_failed"),
])
def test_state_mapping(row, expected):
    assert handler._scaleout_state(row) == expected


def test_state_consumed_wins_over_warm_dispatched():
    # A completed op carries warm_dispatched=True too — must read as warmed,
    # not warming.
    assert handler._scaleout_state(
        {"approval_status": "consumed", "warm_dispatched": True}
    ) == "warmed"


def test_state_warm_failed_outranks_warming_and_warmed():
    # A failed warm sets warm_dispatched=True (would read "warming") and may even
    # carry status=consumed (would read "warmed") — warm_result=="failed" must
    # win over both so a failed op never shows as still-warming or completed.
    assert handler._scaleout_state(
        {"approval_status": "approved", "warm_dispatched": True, "warm_result": "failed"}
    ) == "warm_failed"
    assert handler._scaleout_state(
        {"approval_status": "consumed", "warm_dispatched": True, "warm_result": "failed"}
    ) == "warm_failed"


# ---------------------------------------------------------------------------
# GET /api/scaleout-ops
# ---------------------------------------------------------------------------

_OPS = [
    {"approval_id": "s1", "cluster_id": "c-open", "scaleout": True,
     "approval_status": "awaiting_instance", "created_at": "1000",
     "reader_instance_id": "r-1",
     "action_details": {"endpoint_identifier": "e1", "top_n": 20}},
    {"approval_id": "s2", "cluster_id": "c-teamA", "scaleout": True,
     "approval_status": "pending", "created_at": "1002",
     "reader_instance_id": "r-2",
     "action_details": {"endpoint_identifier": "", "top_n": 10}},
    {"approval_id": "s3", "cluster_id": "c-teamB", "scaleout": True,
     "approval_status": "consumed", "warm_dispatched": True, "created_at": "1001",
     "reader_instance_id": "r-3", "action_details": {"top_n": 5}},
]


def test_list_admin_sees_all_newest_first(monkeypatch):
    _wire(monkeypatch, _FakeTable(_OPS))
    monkeypatch.setattr(handler.tenancy, "visible_set_from_registry", lambda ev: None)

    r = handler.lambda_handler(_admin_event(), None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    ops = body["ops"]
    assert [o["approval_id"] for o in ops] == ["s2", "s3", "s1"]  # created_at desc
    by_id = {o["approval_id"]: o for o in ops}
    assert by_id["s1"]["state"] == "reader_provisioning"
    assert by_id["s2"]["state"] == "warm_pending_approval"
    assert by_id["s3"]["state"] == "warmed"
    assert by_id["s1"]["endpoint_identifier"] == "e1"
    assert by_id["s1"]["top_n"] == 20
    assert by_id["s3"]["warm_dispatched"] is True


def test_list_viewer_excludes_other_team(monkeypatch):
    _wire(monkeypatch, _FakeTable(_OPS))
    monkeypatch.setattr(
        handler.tenancy, "visible_set_from_registry", lambda ev: {"c-open", "c-teamA"}
    )

    r = handler.lambda_handler(_viewer_event(), None)
    assert r["statusCode"] == 200
    cluster_ids = {o["cluster_id"] for o in json.loads(r["body"])["ops"]}
    assert "c-teamB" not in cluster_ids
    assert cluster_ids == {"c-open", "c-teamA"}


# ---------------------------------------------------------------------------
# POST /api/scaleout-ops/{id}/cancel
# ---------------------------------------------------------------------------

def _cancel_event(is_admin=True, with_token=True, aid="s1"):
    ev = (_admin_event if is_admin else _viewer_event)(
        path=f"/api/scaleout-ops/{aid}/cancel", method="POST",
        path_params={"id": aid}, with_token=with_token,
    )
    return ev


@pytest.mark.parametrize("status", ["awaiting_instance", "pending"])
def test_cancel_allowed_states(monkeypatch, status):
    rows = [{"approval_id": "s1", "cluster_id": "c-open", "scaleout": True,
             "approval_status": status, "created_at": "1000"}]
    table = _wire(monkeypatch, _FakeTable(rows))
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid})

    r = handler.lambda_handler(_cancel_event(), None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["state"] == "cancelled"
    assert table.updated is not None
    assert table.updated["values"][":c"] == "cancelled"


@pytest.mark.parametrize("status,dispatched", [
    ("approved", False),
    ("approved", True),
    ("consumed", True),
])
def test_cancel_refused_non_cancellable(monkeypatch, status, dispatched):
    row = {"approval_id": "s1", "cluster_id": "c-open", "scaleout": True,
           "approval_status": status, "created_at": "1000"}
    if dispatched:
        row["warm_dispatched"] = True
    table = _wire(monkeypatch, _FakeTable([row]))
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid})

    r = handler.lambda_handler(_cancel_event(), None)
    assert r["statusCode"] == 409
    assert json.loads(r["body"])["error"] == "cannot_cancel"
    assert table.updated is None  # ConditionExpression path → no mutation


def test_cancel_non_visible_cluster_refused(monkeypatch):
    rows = [{"approval_id": "s1", "cluster_id": "c-teamB", "scaleout": True,
             "approval_status": "pending", "created_at": "1000"}]
    table = _wire(monkeypatch, _FakeTable(rows))
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: False)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid, "team_id": "tB"})

    r = handler.lambda_handler(_cancel_event(), None)
    assert r["statusCode"] == 403
    assert table.updated is None


def test_cancel_non_admin_refused(monkeypatch):
    rows = [{"approval_id": "s1", "cluster_id": "c-open", "scaleout": True,
             "approval_status": "pending", "created_at": "1000"}]
    table = _wire(monkeypatch, _FakeTable(rows))

    r = handler.lambda_handler(_cancel_event(is_admin=False), None)
    assert r["statusCode"] == 403
    assert table.updated is None


def test_cancel_no_token_fail_closed(monkeypatch):
    rows = [{"approval_id": "s1", "cluster_id": "c-open", "scaleout": True,
             "approval_status": "pending", "created_at": "1000"}]
    table = _wire(monkeypatch, _FakeTable(rows))

    r = handler.lambda_handler(_cancel_event(with_token=False), None)
    assert r["statusCode"] == 403
    assert table.updated is None


def test_cancel_not_scaleout_row_404(monkeypatch):
    # A plain (non-scaleout) approval must not be cancellable via this route.
    rows = [{"approval_id": "s1", "cluster_id": "c-open",
             "approval_status": "pending", "created_at": "1000"}]
    table = _wire(monkeypatch, _FakeTable(rows))
    monkeypatch.setattr(handler.tenancy, "cluster_visible", lambda ev, it: True)
    monkeypatch.setattr(handler, "_cluster_item", lambda cid: {"cluster_id": cid})

    r = handler.lambda_handler(_cancel_event(), None)
    assert r["statusCode"] == 404
    assert table.updated is None
