"""Tests for the approval_policies API handler."""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_HANDLER_PATH = Path(__file__).resolve().parents[3] / "api" / "approval_policies" / "handler.py"
_spec = importlib.util.spec_from_file_location("approval_policies_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _jwt(admin=True) -> str:
    payload = {"preferred_username": "alice", "cognito:groups": ["dbops-admin"] if admin else ["dbops-viewer"]}
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def _event(method, body=None, path_id=None, admin=True, bearer=True):
    auth = f"Bearer {_jwt(admin=admin)}" if bearer else _jwt(admin=admin)
    e = {
        "requestContext": {"http": {"method": method}},
        "headers": {"authorization": auth},
        "pathParameters": {"id": path_id} if path_id else {},
    }
    if body is not None:
        e["body"] = json.dumps(body)
    return e


def _fake_table(stored=None):
    store = dict(stored or {})
    t = MagicMock()
    t.scan.return_value = {"Items": list(store.values())}
    t.get_item.side_effect = lambda Key: ({"Item": store[Key["policy_id"]]} if Key["policy_id"] in store else {})
    t.put_item.side_effect = lambda Item: store.__setitem__(Item["policy_id"], Item)
    t.delete_item.side_effect = lambda Key: store.pop(Key["policy_id"], None)
    t._store = store
    return t


def test_viewer_denied_on_every_method():
    for m in ("GET", "POST", "PUT", "DELETE"):
        r = handler.lambda_handler(_event(m, body={}, path_id="x", admin=False))
        assert r["statusCode"] == 403


def test_no_bearer_denied():
    r = handler.lambda_handler(_event("GET", bearer=False))
    assert r["statusCode"] == 403


def test_post_creates_and_normalizes():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("POST", body={
            "cluster_id": "prod-1", "action_type": "execute_sql",
            "approvers": ["  Senior@x.com ", "lead@x.com"], "description": "prod sql",
        }))
    assert r["statusCode"] == 201
    item = json.loads(r["body"])
    assert item["approvers"] == ["lead@x.com", "senior@x.com"]  # trimmed, lowered, sorted
    assert item["policy_id"]
    assert table._store[item["policy_id"]]["cluster_id"] == "prod-1"


def test_post_empty_approvers_rejected():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("POST", body={"approvers": []}))
    assert r["statusCode"] == 400
    assert table._store == {}


def test_post_defaults_wildcards():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("POST", body={"approvers": ["a@x.com"]}))
    item = json.loads(r["body"])
    assert item["cluster_id"] == "*" and item["action_type"] == "*"


def test_get_lists():
    table = _fake_table({"p1": {"policy_id": "p1", "cluster_id": "*", "action_type": "*", "approvers": ["a@x.com"]}})
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("GET"))
    assert r["statusCode"] == 200
    assert len(json.loads(r["body"])["policies"]) == 1


def test_put_updates_existing():
    table = _fake_table({"p1": {"policy_id": "p1", "cluster_id": "*", "action_type": "*", "approvers": ["a@x.com"]}})
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("PUT", body={"approvers": ["b@x.com"]}, path_id="p1"))
    assert r["statusCode"] == 200
    assert table._store["p1"]["approvers"] == ["b@x.com"]


def test_put_missing_404():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("PUT", body={"approvers": ["b@x.com"]}, path_id="nope"))
    assert r["statusCode"] == 404


def test_delete_removes():
    table = _fake_table({"p1": {"policy_id": "p1"}})
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("DELETE", path_id="p1"))
    assert r["statusCode"] == 200
    assert table._store == {}
