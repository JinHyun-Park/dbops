import importlib.util
import json
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "apm_handler", Path(__file__).resolve().parents[3] / "api/apm/handler.py")


def _load():
    mod = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(mod)
    return mod


def _event(method, path_id=None, body=None, admin=True):
    # admin token: cognito:groups=["dbops-admin"]; base64url payload
    import base64
    claims = {"cognito:groups": ["dbops-admin"] if admin else ["team-x"],
              "cognito:username": "hailey"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    tok = f"h.{payload}.s"
    return {
        "requestContext": {"http": {"method": method}},
        "headers": {"Authorization": f"Bearer {tok}"},
        "pathParameters": {"id": path_id} if path_id else {},
        "queryStringParameters": {},
        "body": json.dumps(body) if body else None,
    }


def test_list_targets_returns_200(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_scan_targets", lambda: [
        {"target_id": "svc-a", "service_name": "orders", "team": ""}])
    resp = mod.lambda_handler(_event("GET"), None)
    assert resp["statusCode"] == 200
    assert "svc-a" in resp["body"]


def test_unknown_route_405():
    mod = _load()
    resp = mod.lambda_handler(_event("PATCH"), None)
    assert resp["statusCode"] == 405
