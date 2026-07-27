"""Regression: api/cost, api/explain and api/slack_command must not echo AWS or
driver exception text into a response.

Blast radius, worst first:

  * api/slack_command is a PUBLIC webhook (Slack HMAC only, no Cognito). Its two
    registry lookups returned `str(e)[:200]`, so one DynamoDB/STS failure posted
    the hub account id and the platform IAM role name into a Slack workspace.
  * api/explain is the Query Lab EXPLAIN path. rds-data wraps the engine's
    "ERROR: ..." text TOGETHER with the secret ARN and the cluster ARN in one
    message, and the old "ERROR:" regex fell back to the WHOLE message whenever
    it did not match, so no substring of it was safe to render in a browser.
  * api/cost put Cost Explorer / CloudWatch failure text into the browser-facing
    `note` and into the error strings the views classify on.

Each test injects a fault carrying a fake hub account id, platform role name,
secret ARN and target ARN, then asserts none of it reaches the body WHILE the
control flow that mattered stays put: 400 `sql_error` vs 502 `execution_failed`,
the `cost_allocation_tag_not_activated` classification, and Slack's always-200.
"""

import base64
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]

# api/cost/handler.py does `import tenancy` (a sibling module in its own Lambda
# package), so api/cost has to be importable whether this file runs alone or in
# the full suite.
_COST_DIR = str(ROOT / "api" / "cost")
if _COST_DIR not in sys.path:
    sys.path.insert(0, _COST_DIR)


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


cost = _load("cost_handler_leak", "api/cost/handler.py")
explain = _load("explain_handler_leak", "api/explain/handler.py")
slack = _load("slack_command_handler_leak", "api/slack_command/handler.py")


# ---------------------------------------------------------------------------
# The identifiers a real AWS/rds-data failure carries. None may reach a body.
# ---------------------------------------------------------------------------
HUB_ACCOUNT = "999988887777"
PLATFORM_ROLE = "dbops-prod-api-explain-role"
TARGET_ARN = f"arn:aws:rds:ap-northeast-2:{HUB_ACCOUNT}:cluster:super-secret-prod"
SECRET_ARN = (
    f"arn:aws:secretsmanager:ap-northeast-2:{HUB_ACCOUNT}:secret:dbops-prod-cache-AbCdEf"
)

# rds-data's wrapper for anything the ENGINE rejected: the useful "ERROR: ..."
# part and the ARNs arrive in the same string.
SQL_FAULT = (
    "An error occurred (BadRequestException) when calling the ExecuteStatement "
    'operation: ERROR: relation "salaries" does not exist; Position: 15; '
    f"SQLState: 42P01 (resourceArn={TARGET_ARN}, secretArn={SECRET_ARN})"
)

# An infrastructure failure: IAM, and it spells out the platform role.
IAM_FAULT = (
    "An error occurred (AccessDeniedException) when calling the operation: User: "
    f"arn:aws:sts::{HUB_ACCOUNT}:assumed-role/{PLATFORM_ROLE}/session is not "
    f"authorized to perform: rds-data:ExecuteStatement on resource: {TARGET_ARN}"
)


class _Fault(RuntimeError):
    """Every injected fault is this class, so the tests also catch a payload that
    renders the exception CLASS name (`type(e).__name__`). A class name is not
    free text, but it is internals a browser has no use for, and the tokens view
    used to put it in the operator-facing `note`."""


_FORBIDDEN = (
    "_Fault",
    HUB_ACCOUNT,
    PLATFORM_ROLE,
    TARGET_ARN,
    SECRET_ARN,
    "secretsmanager",
    "assumed-role",
    "not authorized to perform",
    "SQLState",
    "salaries",          # the engine's own text, which carried the ARNs with it
    "AccessDeniedException",
    "BadRequestException",
)


def _assert_clean(blob: str, where: str):
    for needle in _FORBIDDEN:
        assert needle not in blob, f"{needle!r} leaked into {where}"


START, END = date(2026, 5, 1), date(2026, 5, 31)


def _get_event(view: str) -> dict:
    return {
        "requestContext": {"http": {"method": "GET"}},
        "queryStringParameters": {"view": view, "days": "30"},
    }


# ---------------------------------------------------------------------------
# api/explain: the 400/502 split is control flow and must survive
# ---------------------------------------------------------------------------

def _explain(monkeypatch, fault: str) -> dict:
    monkeypatch.setattr(explain, "_lookup_cluster", lambda cid: {
        "cluster_arn": TARGET_ARN, "secret_arn": SECRET_ARN,
        "db_name": "app", "engine": "aurora-postgresql",
    })
    client = MagicMock()
    client.execute_statement.side_effect = _Fault(fault)
    monkeypatch.setattr(explain.boto3, "client", lambda *_a, **_k: client)
    # A real admin token: EXPLAIN ANALYZE is admin-gated, and _is_admin only
    # reads the (already API-Gateway-verified) claims.
    claims = base64.urlsafe_b64encode(
        json.dumps({"cognito:groups": ["dbops-admin"]}).encode()
    ).decode().rstrip("=")
    return explain.lambda_handler({
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"authorization": f"Bearer header.{claims}.sig"},
        "body": json.dumps({"cluster_id": "prod-pg", "sql": "SELECT 1"}),
    }, None)


def test_explain_sql_error_stays_400_and_carries_no_exception_text(monkeypatch):
    resp = _explain(monkeypatch, SQL_FAULT)
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert body["error"] == "sql_error"
    _assert_clean(resp["body"], "explain sql_error response")


def test_explain_sql_error_message_is_still_actionable(monkeypatch):
    """Scrubbed, not silent: the user learns the STATEMENT was rejected (their
    fault) and what to check, and still gets back the SQL we ran."""
    body = json.loads(_explain(monkeypatch, SQL_FAULT)["body"])
    assert "구문" in body["message"]
    assert body["explain_sql"].startswith("EXPLAIN")


def test_explain_infrastructure_error_stays_502_and_is_clean(monkeypatch):
    resp = _explain(monkeypatch, IAM_FAULT)
    assert resp["statusCode"] == 502
    body = json.loads(resp["body"])
    assert body["error"] == "execution_failed"
    _assert_clean(resp["body"], "explain execution_failed response")


def test_explain_keeps_the_two_failure_classes_distinguishable(monkeypatch):
    """The messages are static, but a SQL error and an infra error must not read
    the same: that distinction is what tells the user whose fault it is."""
    sql_msg = json.loads(_explain(monkeypatch, SQL_FAULT)["body"])["message"]
    infra_msg = json.loads(_explain(monkeypatch, IAM_FAULT)["body"])["message"]
    assert sql_msg != infra_msg


# ---------------------------------------------------------------------------
# api/slack_command: public webhook, widest blast radius
# ---------------------------------------------------------------------------

@pytest.fixture
def _slack_env(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")
    boom = MagicMock()
    boom.resource.side_effect = _Fault(IAM_FAULT)
    monkeypatch.setattr(slack, "boto3", boom)


def test_slack_status_lookup_failure_is_clean(_slack_env):
    resp = slack._cmd_status(["prod-pg"])
    assert resp["statusCode"] == 200  # 200 keeps Slack from retrying
    _assert_clean(resp["body"], "/dbops status failure")
    # Echoing back the cluster id the caller typed is their own input, not a leak,
    # and it is the only way they know WHICH lookup failed.
    assert "prod-pg" in json.loads(resp["body"])["text"]


def test_slack_clusters_scan_failure_is_clean(_slack_env):
    resp = slack._cmd_clusters()
    assert resp["statusCode"] == 200
    _assert_clean(resp["body"], "/dbops clusters failure")


# ---------------------------------------------------------------------------
# api/cost: CE error tokens stay BOUNDED, the tokens-view note stays static
# ---------------------------------------------------------------------------

def test_query_total_failure_returns_a_bounded_token():
    ce = MagicMock()
    ce.get_cost_and_usage.side_effect = _Fault(IAM_FAULT)
    daily, total, err = cost._query_total(ce, START, END, ["Amazon Bedrock"])
    assert (daily, total) == ([], 0.0)
    assert err == "cost_explorer_query_failed"


def test_query_total_still_classifies_an_unactivated_tag():
    """The one branch callers depend on: the token drives no_data_reason."""
    ce = MagicMock()
    ce.get_cost_and_usage.side_effect = _Fault(
        "The tag Application is not currently activated for cost allocation"
    )
    _daily, _total, err = cost._query_total(ce, START, END, ["Amazon Bedrock"])
    assert err == "cost_allocation_tag_not_activated"


def test_query_by_dimension_error_token_is_clean():
    ce = MagicMock()
    ce.get_cost_and_usage.side_effect = _Fault(IAM_FAULT)
    rows, err = cost._query_by_dimension(ce, START, END, ["Amazon RDS"], "USAGE_TYPE")
    assert rows == []
    _assert_clean(err, "_query_by_dimension error")


def test_per_cluster_error_token_is_clean_but_keeps_the_not_activated_flag():
    ce = MagicMock()
    ce.get_cost_and_usage.side_effect = _Fault(IAM_FAULT)
    _rows, _tag, err = cost._query_per_cluster(ce, START, END, ["Amazon RDS"])
    _assert_clean(err, "_query_per_cluster error")

    ce.get_cost_and_usage.side_effect = _Fault("tag is not currently activated")
    _rows, _tag, err = cost._query_per_cluster(ce, START, END, ["Amazon RDS"])
    assert err == "cost_allocation_tag_not_activated"


def test_platform_view_activation_guidance_survives():
    """no_data_reason is derived from the token, not from the exception."""
    ce = MagicMock()
    ce.get_cost_and_usage.side_effect = _Fault(
        "The tag Application is not currently activated for cost allocation. "
        + IAM_FAULT
    )
    resp = cost._handle_platform_view(ce, START, END, 30)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert "활성화" in body["no_data_reason"]
    _assert_clean(resp["body"], "platform view response")


def test_tokens_view_list_metrics_failure_note_is_static(monkeypatch):
    cw = MagicMock()
    cw.list_metrics.side_effect = _Fault(IAM_FAULT)
    monkeypatch.setattr(cost.boto3, "client", lambda *_a, **_k: cw)
    resp = cost.lambda_handler(_get_event("tokens"))
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["by_model"] == [] and body["daily"] == []
    assert body["note"]
    _assert_clean(resp["body"], "tokens list_metrics note")


def test_tokens_view_get_metric_data_failure_note_is_static(monkeypatch):
    cw = MagicMock()
    cw.list_metrics.return_value = {
        "Metrics": [{"Dimensions": [{"Name": "ModelId", "Value": "anthropic.claude-x"}]}]
    }
    cw.get_metric_data.side_effect = _Fault(IAM_FAULT)
    monkeypatch.setattr(cost.boto3, "client", lambda *_a, **_k: cw)
    resp = cost.lambda_handler(_get_event("tokens"))
    assert resp["statusCode"] == 200
    _assert_clean(resp["body"], "tokens get_metric_data note")


def _tokens_note(monkeypatch, cw) -> str:
    monkeypatch.setattr(cost.boto3, "client", lambda *_a, **_k: cw)
    return json.loads(cost._handle_tokens_view(START, END, 30)["body"])["note"]


def test_tokens_view_notes_name_the_failing_step(monkeypatch):
    """Both notes are static, so they must still differ: 'cannot list the token
    metrics' and 'listed them, could not read the data' are different operator
    actions, and the message was the only thing carrying that distinction."""
    listing_failed = MagicMock()
    listing_failed.list_metrics.side_effect = _Fault(IAM_FAULT)

    read_failed = MagicMock()
    read_failed.list_metrics.return_value = {
        "Metrics": [{"Dimensions": [{"Name": "ModelId", "Value": "anthropic.claude-x"}]}]
    }
    read_failed.get_metric_data.side_effect = _Fault(IAM_FAULT)

    assert _tokens_note(monkeypatch, listing_failed) != _tokens_note(monkeypatch, read_failed)
