"""Tests for the onboarding API handler."""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_HANDLER_PATH = Path(__file__).resolve().parents[3] / "api" / "onboarding" / "handler.py"
_spec = importlib.util.spec_from_file_location("onboarding_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

_ACCOUNT = "111122223333"


def _jwt(user="alice", admin=True, viewer_only=False) -> str:
    if viewer_only:
        groups = ["dbops-viewer"]
    else:
        groups = ["dbops-admin"] if admin else []
    payload = {
        "preferred_username": user,
        "cognito:groups": groups,
    }
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def _event(qs=None, admin=True, viewer_only=False, no_bearer=False):
    token = _jwt(admin=admin, viewer_only=viewer_only)
    auth = token if no_bearer else f"Bearer {token}"
    e = {
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"authorization": auth},
    }
    if qs:
        e["queryStringParameters"] = qs
    return e


def _mock_sts(monkeypatch):
    mock_client = MagicMock()
    mock_client.get_caller_identity.return_value = {"Account": _ACCOUNT}
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_client
    monkeypatch.setattr(handler, "boto3", mock_boto3)
    return mock_boto3


def test_template_read_only_default(monkeypatch):
    _mock_sts(monkeypatch)
    r = handler.lambda_handler(_event())
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    # Parse the nested template JSON string
    tmpl = json.loads(body["template"])

    # Role name
    props = tmpl["Resources"]["DBOpsSpokeRole"]["Properties"]
    assert props["RoleName"] == "dbops-spoke-role"

    # Trust principal — hub account root
    trust_stmt = props["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust_stmt["Principal"]["AWS"] == f"arn:aws:iam::{_ACCOUNT}:root"

    # Inline policy statements
    policy_stmts = props["Policies"][0]["PolicyDocument"]["Statement"]
    all_actions = []
    for stmt in policy_stmts:
        actions = stmt["Action"]
        if isinstance(actions, list):
            all_actions.extend(actions)
        else:
            all_actions.append(actions)

    # A read action must be present
    assert "rds:Describe*" in all_actions

    # No write action in read-only mode
    assert "rds:ModifyDBCluster" not in all_actions

    # Top-level response fields
    assert body["remediation"] is False
    assert body["hub_account_id"] == _ACCOUNT
    assert body["role_name"] == "dbops-spoke-role"


def test_template_remediation_adds_write(monkeypatch):
    _mock_sts(monkeypatch)
    r = handler.lambda_handler(_event(qs={"remediation": "true"}))
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    tmpl = json.loads(body["template"])

    props = tmpl["Resources"]["DBOpsSpokeRole"]["Properties"]
    policy_stmts = props["Policies"][0]["PolicyDocument"]["Statement"]
    all_actions = []
    for stmt in policy_stmts:
        actions = stmt["Action"]
        if isinstance(actions, list):
            all_actions.extend(actions)
        else:
            all_actions.append(actions)

    # Write action is added by remediation flag
    assert "rds:ModifyDBCluster" in all_actions
    # Remediation is ADDITIVE — read actions are still present
    assert "rds:Describe*" in all_actions
    assert body["remediation"] is True


def test_viewer_denied():
    r = handler.lambda_handler(_event(viewer_only=True))
    assert r["statusCode"] == 403


def test_no_bearer_denied():
    r = handler.lambda_handler(_event(no_bearer=True))
    assert r["statusCode"] == 403


def test_options_bypasses_auth():
    e = {
        "requestContext": {"http": {"method": "OPTIONS"}},
        "headers": {},
    }
    r = handler.lambda_handler(e)
    assert r["statusCode"] == 200


def test_region_passthrough(monkeypatch):
    _mock_sts(monkeypatch)
    r = handler.lambda_handler(_event(qs={"region": "us-west-2"}))
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["region"] == "us-west-2"


def test_no_auth_header_denied():
    e = {
        "requestContext": {"http": {"method": "GET"}},
        "headers": {},
    }
    r = handler.lambda_handler(e)
    assert r["statusCode"] == 403


def test_garbage_token_denied():
    # Bearer prefix present but payload is not a valid JWT — decodes to empty claims → 403.
    e = {
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": "Bearer notajwt"},
    }
    r = handler.lambda_handler(e)
    assert r["statusCode"] == 403
