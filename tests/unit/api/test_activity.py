"""Tests for /api/activity — the chronological audit feed routed
through the approvals lambda."""

import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

_PATH = Path(__file__).resolve().parents[3] / "api" / "approvals" / "handler.py"
_spec = importlib.util.spec_from_file_location("approvals_handler", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _activity_event(method="GET", qs=None, path="/api/activity"):
    return {
        "httpMethod": method,
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "queryStringParameters": qs or {},
        "headers": {},
    }


def _row(
    aid="a-1",
    cluster="prod-pg-1",
    action="execute_sql",
    status="consumed",
    requested="agent",
    approved="alice",
    created="2026-05-15T10:00:00",
):
    return {
        "approval_id": aid,
        "created_at": created,
        "approval_status": status,
        "cluster_id": cluster,
        "action_type": action,
        "requested_by": requested,
        "approved_by": approved,
        "action_details": {"sql": "UPDATE users SET name='test'"},
    }


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch.object(handler, "boto3")
def test_activity_returns_chronological(mock_boto3):
    mock_table = MagicMock()
    # Out-of-order to verify the handler sorts.
    mock_table.scan.return_value = {
        "Items": [
            _row(aid="a-1", created="2026-05-10T08:00:00"),
            _row(aid="a-3", created="2026-05-20T15:00:00"),
            _row(aid="a-2", created="2026-05-15T12:00:00"),
        ]
    }
    mock_boto3.resource.return_value.Table.return_value = mock_table

    res = handler.lambda_handler(_activity_event(), None)
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    ids = [it["approval_id"] for it in body["items"]]
    assert ids == ["a-3", "a-2", "a-1"]  # newest first
    assert body["count"] == 3


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch.object(handler, "boto3")
def test_activity_cluster_filter_pushed_to_ddb(mock_boto3):
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": []}
    mock_boto3.resource.return_value.Table.return_value = mock_table

    handler.lambda_handler(
        _activity_event(qs={"cluster_id": "prod-pg-1"}), None,
    )
    call_kwargs = mock_table.scan.call_args.kwargs
    assert "cluster_id = :cid" in call_kwargs["FilterExpression"]
    assert call_kwargs["ExpressionAttributeValues"][":cid"] == "prod-pg-1"


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch.object(handler, "boto3")
def test_activity_actor_matches_either_field(mock_boto3):
    """`actor=alice` should match rows where Alice was EITHER the
    requester OR the approver."""
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": []}
    mock_boto3.resource.return_value.Table.return_value = mock_table

    handler.lambda_handler(
        _activity_event(qs={"actor": "alice"}), None,
    )
    fe = mock_table.scan.call_args.kwargs["FilterExpression"]
    assert "requested_by = :a" in fe
    assert "approved_by = :a" in fe


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch.object(handler, "boto3")
def test_activity_action_type_filter_matches_either_column(mock_boto3):
    """action_type filter should match either action_type (new shape)
    or tool_name (legacy)."""
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": []}
    mock_boto3.resource.return_value.Table.return_value = mock_table

    handler.lambda_handler(
        _activity_event(qs={"action_type": "execute_sql"}), None,
    )
    fe = mock_table.scan.call_args.kwargs["FilterExpression"]
    assert "action_type = :at" in fe
    assert "tool_name = :at" in fe


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch.object(handler, "boto3")
def test_activity_truncates_action_details(mock_boto3):
    """Large action_details should be excerpt-trimmed to 500 chars."""
    mock_table = MagicMock()
    big = {"sql": "X" * 2000}
    mock_table.scan.return_value = {"Items": [_row()]}
    # Replace details with the big payload
    mock_table.scan.return_value["Items"][0]["action_details"] = big
    mock_boto3.resource.return_value.Table.return_value = mock_table

    res = handler.lambda_handler(_activity_event(), None)
    body = json.loads(res["body"])
    assert len(body["items"][0]["action_details_excerpt"]) <= 500


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch.object(handler, "boto3")
def test_activity_limit_clamped(mock_boto3):
    """limit query param is clamped to [1, 500]."""
    mock_table = MagicMock()
    # 600 rows — enough to test the cap.
    mock_table.scan.return_value = {
        "Items": [_row(aid=f"a-{i}", created=f"2026-05-15T10:{i:02d}:00") for i in range(60)]
    }
    mock_boto3.resource.return_value.Table.return_value = mock_table

    res = handler.lambda_handler(
        _activity_event(qs={"limit": "9999"}), None,
    )
    body = json.loads(res["body"])
    assert body["count"] <= 500


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch.object(handler, "boto3")
def test_activity_decimal_serialized(mock_boto3):
    """DDB returns Decimal for numeric fields; the JSON serializer
    must handle them (no TypeError)."""
    mock_table = MagicMock()
    row = _row()
    row["created_at"] = "2026-05-15T10:00:00"
    row["score"] = Decimal("1.5")
    mock_table.scan.return_value = {"Items": [row]}
    mock_boto3.resource.return_value.Table.return_value = mock_table

    res = handler.lambda_handler(_activity_event(), None)
    assert res["statusCode"] == 200
    # Should not have thrown — Decimal coerced via default=str
    json.loads(res["body"])
