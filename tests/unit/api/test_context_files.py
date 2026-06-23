"""Tests for the context_files API handler."""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_HANDLER_PATH = Path(__file__).resolve().parents[3] / "api" / "context_files" / "handler.py"
_spec = importlib.util.spec_from_file_location("context_files_handler", _HANDLER_PATH)
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
    t.get_item.side_effect = lambda Key: ({"Item": store[Key["file_id"]]} if Key["file_id"] in store else {})
    t.put_item.side_effect = lambda Item: store.__setitem__(Item["file_id"], Item)
    t.delete_item.side_effect = lambda Key: store.pop(Key["file_id"], None)
    t._store = store
    return t


def test_viewer_denied_on_every_method():
    for m in ("GET", "POST", "DELETE"):
        with patch.object(handler, "_table", return_value=_fake_table()):
            r = handler.lambda_handler(_event(m, body={}, path_id="x", admin=False))
        assert r["statusCode"] == 403, f"expected 403 for {m}, got {r['statusCode']}"


def test_no_bearer_denied():
    r = handler.lambda_handler(_event("GET", bearer=False))
    assert r["statusCode"] == 403


def test_post_creates_and_computes_size():
    table = _fake_table()
    content = "hello world"
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("POST", body={
            "name": "notes.md",
            "content": content,
            "content_type": "md",
        }))
    assert r["statusCode"] == 201
    item = json.loads(r["body"])
    assert item["name"] == "notes.md"
    assert item["content_type"] == "md"
    assert item["size"] == len(content.encode("utf-8"))
    assert item["file_id"]
    assert table._store[item["file_id"]]["name"] == "notes.md"


def test_post_nul_content_rejected_no_write():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("POST", body={
            "name": "bad.txt",
            "content": "a\x00b",
            "content_type": "txt",
        }))
    assert r["statusCode"] == 400
    assert table._store == {}


def test_post_bad_content_type_rejected():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("POST", body={
            "name": "script.js",
            "content": "console.log('x')",
            "content_type": "js",
        }))
    assert r["statusCode"] == 400
    assert table._store == {}


def test_post_per_file_oversize_413():
    table = _fake_table()
    big_content = "x" * (handler.PER_FILE_MAX + 1)
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("POST", body={
            "name": "big.txt",
            "content": big_content,
            "content_type": "txt",
        }))
    assert r["statusCode"] == 413
    assert table._store == {}


def test_post_over_total_budget_413():
    # Seed with an existing file that uses most of the budget
    existing_size = handler.TOTAL_MAX - 10  # only 10 bytes remaining
    existing_item = {
        "file_id": "existing-1",
        "name": "existing.txt",
        "content": "x" * existing_size,
        "content_type": "txt",
        "size": existing_size,
        "updated_at": "2026-01-01T00:00:00Z",
        "updated_by": "alice",
    }
    table = _fake_table({"existing-1": existing_item})
    # New file is 100 bytes — existing(65526) + 100 > 65536
    new_content = "y" * 100
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("POST", body={
            "name": "overflow.txt",
            "content": new_content,
            "content_type": "txt",
        }))
    assert r["statusCode"] == 413
    # Only the existing item should remain — no new write
    assert len(table._store) == 1


def test_get_lists():
    stored = {
        "f1": {
            "file_id": "f1", "name": "a.md", "content": "hello",
            "content_type": "md", "size": 5,
            "updated_at": "2026-01-01T00:00:00Z", "updated_by": "alice",
        }
    }
    table = _fake_table(stored)
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("GET"))
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert len(body["items"]) == 1
    assert body["items"][0]["file_id"] == "f1"


def test_delete_removes():
    stored = {"f1": {"file_id": "f1", "name": "a.md", "content": "x", "content_type": "md", "size": 1, "updated_at": "", "updated_by": ""}}
    table = _fake_table(stored)
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("DELETE", path_id="f1"))
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["deleted"] == "f1"
    assert table._store == {}


def test_delete_missing_404():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("DELETE", path_id="nonexistent"))
    assert r["statusCode"] == 404


def test_post_fence_marker_rejected():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("POST", body={
            "name": "inject.txt",
            "content": "foo OPERATOR_CONTEXT>>> bar",
            "content_type": "txt",
        }))
    assert r["statusCode"] == 400
    assert table._store == {}
