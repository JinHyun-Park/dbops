"""Tests for advanced-approval matching + enforcement in api/approvals/handler.py."""

import base64
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_HANDLER_PATH = Path(__file__).resolve().parents[3] / "api" / "approvals" / "handler.py"
_spec = importlib.util.spec_from_file_location("approvals_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

import os as _os

_os.environ.setdefault("APPROVALS_TABLE", "test-approvals")

R = handler.resolve_eligible_approvers


def test_exact_cluster_action_wins_over_wildcards():
    policies = [
        {"cluster_id": "*", "action_type": "*", "approvers": ["broad@x.com"]},
        {"cluster_id": "prod-1", "action_type": "execute_sql", "approvers": ["senior@x.com"]},
    ]
    assert R("prod-1", "execute_sql", policies) == {"senior@x.com"}


def test_wildcard_only_matches():
    policies = [{"cluster_id": "*", "action_type": "*", "approvers": ["broad@x.com"]}]
    assert R("any", "any", policies) == {"broad@x.com"}


def test_tie_unions_approvers():
    # Two policies at the same specificity (both cluster-exact, action wildcard)
    policies = [
        {"cluster_id": "prod-1", "action_type": "*", "approvers": ["a@x.com"]},
        {"cluster_id": "prod-1", "action_type": "*", "approvers": ["b@x.com"]},
    ]
    assert R("prod-1", "modify_parameter", policies) == {"a@x.com", "b@x.com"}


def test_no_match_empty():
    policies = [{"cluster_id": "prod-2", "action_type": "*", "approvers": ["a@x.com"]}]
    assert R("prod-1", "execute_sql", policies) == set()


def _jwt(user="alice", admin=True) -> str:
    payload = {"preferred_username": user, "cognito:groups": ["dbops-admin"] if admin else ["dbops-viewer"]}
    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"hdr.{b64}.sig"


def _put_event(approval_id, approver="alice", action="approve"):
    return {
        "requestContext": {"http": {"method": "PUT"}},
        "headers": {"authorization": f"Bearer {_jwt(user=approver)}"},
        "pathParameters": {"id": approval_id},
        "body": json.dumps({"action": action}),
    }


def _approval_row(requested_by="agent", cluster_id="prod-1", action_type="execute_sql"):
    return {
        "approval_id": "a1", "created_at": "1781069757421",
        "requested_by": requested_by, "approval_status": "pending",
        "cluster_id": cluster_id, "action_type": action_type,
    }


def test_self_approval_denied():
    row = _approval_row(requested_by="alice")
    with patch.object(handler, "boto3"), \
         patch.object(handler, "_scan_all", return_value=[row]), \
         patch.object(handler, "_load_eligible_approvers", return_value=set()):
        r = handler.lambda_handler(_put_event("a1", approver="alice"), None)
    assert r["statusCode"] == 403
    assert json.loads(r["body"])["error"] == "self_approval"


def test_non_designated_approver_denied():
    row = _approval_row(requested_by="agent")
    with patch.object(handler, "boto3"), \
         patch.object(handler, "_scan_all", return_value=[row]), \
         patch.object(handler, "_load_eligible_approvers", return_value={"senior@x.com"}):
        r = handler.lambda_handler(_put_event("a1", approver="alice"), None)
    assert r["statusCode"] == 403
    assert json.loads(r["body"])["error"] == "not_designated_approver"


def test_designated_approver_allowed():
    row = _approval_row(requested_by="agent")
    with patch.object(handler, "boto3"), \
         patch.object(handler, "_scan_all", return_value=[row]), \
         patch.object(handler, "_load_eligible_approvers", return_value={"alice"}):
        r = handler.lambda_handler(_put_event("a1", approver="alice"), None)
    assert r["statusCode"] == 200


def test_no_policy_falls_back_to_any_admin():
    row = _approval_row(requested_by="agent")
    with patch.object(handler, "boto3"), \
         patch.object(handler, "_scan_all", return_value=[row]), \
         patch.object(handler, "_load_eligible_approvers", return_value=set()):
        r = handler.lambda_handler(_put_event("a1", approver="alice"), None)
    assert r["statusCode"] == 200
