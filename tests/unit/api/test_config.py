"""Tests for the config API handler."""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_HANDLER_PATH = Path(__file__).resolve().parents[3] / "api" / "config" / "handler.py"
_spec = importlib.util.spec_from_file_location("config_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _jwt(user="alice", admin=True) -> str:
    payload = {
        "preferred_username": user,
        "cognito:groups": ["dbops-admin"] if admin else ["dbops-viewer"],
    }
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def _event(method, body=None, admin=True):
    e = {
        "requestContext": {"http": {"method": method}},
        "headers": {"authorization": f"Bearer {_jwt(admin=admin)}"},
    }
    if body is not None:
        e["body"] = json.dumps(body)
    return e


def _fake_table(stored=None):
    store = dict(stored or {})
    t = MagicMock()
    t.get_item.side_effect = lambda Key: (
        {"Item": store[Key["config_key"]]} if Key["config_key"] in store else {}
    )

    def _put(Item):
        store[Item["config_key"]] = Item
    t.put_item.side_effect = _put
    t._store = store
    return t


def test_get_returns_defaults_when_empty():
    with patch.object(handler, "_table", return_value=_fake_table()):
        r = handler.lambda_handler(_event("GET"))
    assert r["statusCode"] == 200
    items = {i["key"]: i for i in json.loads(r["body"])["items"]}
    assert items["TICKETING_PROVIDER"]["value"] == "none"
    assert items["REPORT_DELIVERY_ENABLED"]["value"] == "false"
    assert items["TICKETING_PROVIDER"]["updated_at"] is None


def test_get_viewer_denied():
    r = handler.lambda_handler(_event("GET", admin=False))
    assert r["statusCode"] == 403


def test_put_persists_and_normalizes_bool():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("PUT", {"config": {"REPORT_DELIVERY_ENABLED": True}}))
    assert r["statusCode"] == 200
    assert table._store["REPORT_DELIVERY_ENABLED"]["value"] == "true"
    items = {i["key"]: i for i in json.loads(r["body"])["items"]}
    assert items["REPORT_DELIVERY_ENABLED"]["value"] == "true"
    assert items["REPORT_DELIVERY_ENABLED"]["updated_by"] == "alice"


def test_put_unknown_key_rejected_no_write():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("PUT", {"config": {"BOGUS": "x"}}))
    assert r["statusCode"] == 400
    assert table._store == {}


def test_put_bad_provider_format_rejected():
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("PUT", {"config": {"TICKETING_PROVIDER": "Has Space!"}}))
    assert r["statusCode"] == 400
    assert table._store == {}


def test_put_viewer_denied():
    r = handler.lambda_handler(_event("PUT", {"config": {"REPORT_DELIVERY_ENABLED": True}}, admin=False))
    assert r["statusCode"] == 403


def test_options_bypasses_auth():
    # CORS preflight must pass even for non-admin tokens — no auth gate before OPTIONS return.
    r = handler.lambda_handler(_event("OPTIONS", admin=False))
    assert r["statusCode"] == 200


def test_put_malformed_json_body_400():
    # Build the event inline (not via _event) so body is a raw non-JSON string.
    e = {
        "requestContext": {"http": {"method": "PUT"}},
        "headers": {"authorization": f"Bearer {_jwt(admin=True)}"},
        "body": "{not json",
    }
    r = handler.lambda_handler(e)
    assert r["statusCode"] == 400


def test_put_missing_config_key_400():
    # Empty body dict has no "config" key — must be rejected and nothing written.
    table = _fake_table()
    with patch.object(handler, "_table", return_value=table):
        r = handler.lambda_handler(_event("PUT", {}))
    assert r["statusCode"] == 400
    assert table._store == {}
