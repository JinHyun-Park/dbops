"""Tests for the server-side approval guard.

These tests cover every reason the guard can reject — missing id, wrong
cluster, wrong action_type, stale resolved_at, status not approved,
already-consumed, missing resolved_at, missing env var. The atomic
consume path is also tested via the ConditionalCheckFailedException
branch.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from mcp_servers.shared.approval_guard import (
    REPLAY_WINDOW_SECONDS,
    canonical_action_hash,
    verify_approval,
)


def _iso_now(delta_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


def _fresh_row(approval_id="aid-1", cluster_id="prod-pg-1", action_type="execute_sql", status="approved"):
    return {
        "approval_id": approval_id,
        "created_at": "1716903000000",
        "approval_status": status,
        "cluster_id": cluster_id,
        "action_type": action_type,
        "resolved_at": _iso_now(),
    }


def _scan_returning(item):
    """Build a mock DDB table that returns `item` for scan() and accepts
    update_item() without raising."""
    table = MagicMock()
    table.scan.return_value = {"Items": [item]} if item else {"Items": []}
    return table


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_happy_path(mock_boto3):
    table = _scan_returning(_fresh_row())
    mock_boto3.resource.return_value.Table.return_value = table

    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")

    assert result == {"ok": True}
    # Atomic consume must be called with the conditional check.
    table.update_item.assert_called_once()
    call = table.update_item.call_args.kwargs
    assert "ConditionExpression" in call
    assert ":approved" in call["ExpressionAttributeValues"]


def test_verify_approval_missing_id_rejects():
    result = verify_approval("", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "approval_id missing" in result["reason"]


@patch.dict("os.environ", {}, clear=True)
def test_verify_approval_missing_table_env_fails_closed():
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "APPROVALS_TABLE" in result["reason"]


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"}, clear=True)
def test_verify_approval_bypass_env_for_local_dev():
    """The bypass env var lets local dev skip approval but must NOT be
    settable from the agent. It's gated by the deployment, not the call."""
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is True
    assert result.get("bypass") is True


@patch.dict(
    "os.environ",
    {"APPROVAL_GUARD_BYPASS": "1", "AWS_LAMBDA_FUNCTION_NAME": "dbops-prod-OperationsMCP"},
    clear=True,
)
def test_bypass_refused_inside_lambda_runtime():
    """Even if APPROVAL_GUARD_BYPASS leaks onto a deployed Lambda, the guard
    must NOT honor it — approvals stay enforced in production. With no
    APPROVALS_TABLE set, the bypass being refused means we fall through to the
    fail-closed 'table not configured' path rather than returning ok."""
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "bypass" not in result


@patch.dict(
    "os.environ",
    {"APPROVAL_GUARD_BYPASS": "1", "AWS_EXECUTION_ENV": "AWS_Lambda_python3.12"},
    clear=True,
)
def test_bypass_refused_when_aws_execution_env_present():
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_unknown_id(mock_boto3):
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(None)
    result = verify_approval("aid-missing", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "not found" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_status_pending_rejected(mock_boto3):
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(
        _fresh_row(status="pending")
    )
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "pending" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_status_rejected_path(mock_boto3):
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(
        _fresh_row(status="rejected")
    )
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "rejected" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_already_consumed(mock_boto3):
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(
        _fresh_row(status="consumed")
    )
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "consumed" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_wrong_cluster(mock_boto3):
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(
        _fresh_row(cluster_id="prod-pg-1")
    )
    result = verify_approval("aid-1", "DIFFERENT-cluster", "execute_sql")
    assert result["ok"] is False
    assert "prod-pg-1" in result["reason"]
    assert "DIFFERENT-cluster" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_wrong_action_type(mock_boto3):
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(
        _fresh_row(action_type="modify_parameter")
    )
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "modify_parameter" in result["reason"]
    assert "execute_sql" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_action_type_other_is_permissive(mock_boto3):
    """If the approval row was registered with action_type='other' the
    tool's specific action_type still has to match — but if both sides
    say 'other', it passes."""
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(
        _fresh_row(action_type="other")
    )
    result = verify_approval("aid-1", "prod-pg-1", "other")
    assert result["ok"] is True


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_stale_rejected(mock_boto3):
    row = _fresh_row()
    row["resolved_at"] = _iso_now(delta_seconds=-(REPLAY_WINDOW_SECONDS + 60))
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(row)

    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "old" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_no_resolved_at(mock_boto3):
    row = _fresh_row()
    row["resolved_at"] = ""
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(row)
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "resolved_at" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_garbage_resolved_at(mock_boto3):
    row = _fresh_row()
    row["resolved_at"] = "not-a-date"
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(row)
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_concurrent_consume_loses(mock_boto3):
    """The atomic consume can race — if another process consumed first,
    we get ConditionalCheckFailedException and must reject."""
    table = _scan_returning(_fresh_row())
    err = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "race"}},
        "UpdateItem",
    )
    table.update_item.side_effect = err
    mock_boto3.resource.return_value.Table.return_value = table

    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "concurrent" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_zulu_timestamp_parses(mock_boto3):
    """resolved_at often ends with Z when the writer uses utcnow().isoformat()
    + 'Z'. The guard must parse this correctly."""
    row = _fresh_row()
    row["resolved_at"] = "2026-05-28T10:00:00Z"
    # Make resolved_at "now" by patching time.time inside the guard.
    with patch("mcp_servers.shared.approval_guard.time.time") as mock_time:
        # Pretend it's only 60s after the resolved_at timestamp.
        from datetime import datetime as _dt
        mock_time.return_value = _dt.fromisoformat("2026-05-28T10:01:00+00:00").timestamp()
        mock_boto3.resource.return_value.Table.return_value = _scan_returning(row)

        result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
        assert result["ok"] is True


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_verify_approval_ddb_scan_error_rejected(mock_boto3):
    """If DDB itself errors during scan, fail-closed (don't execute write)."""
    table = MagicMock()
    table.scan.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError"}}, "Scan",
    )
    mock_boto3.resource.return_value.Table.return_value = table

    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "not found" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_find_approval_paginates_past_nonmatching_pages(mock_boto3):
    """Regression: scan used Limit=1, but DynamoDB applies Limit BEFORE
    FilterExpression — with 2+ rows in the table the matching approval was
    never found and every approved write was refused. The lookup must follow
    LastEvaluatedKey across pages and must not pass Limit."""
    table = MagicMock()
    table.scan.side_effect = [
        {"Items": [], "LastEvaluatedKey": {"approval_id": "other"}},
        {"Items": [_fresh_row()]},
    ]
    mock_boto3.resource.return_value.Table.return_value = table

    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")

    assert result == {"ok": True}
    assert table.scan.call_count == 2
    for call in table.scan.call_args_list:
        assert "Limit" not in call.kwargs
    assert table.scan.call_args_list[1].kwargs["ExclusiveStartKey"] == {"approval_id": "other"}


# ===== Payload binding (the approval is tied to the exact operation) =====


def _row_with_hash(action_type, details, **kw):
    row = _fresh_row(action_type=action_type, **kw)
    row["payload_hash"] = canonical_action_hash(action_type, details)
    return row


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_payload_match_passes(mock_boto3):
    row = _row_with_hash("execute_sql", {"sql": "UPDATE t SET x=1 WHERE id=5"})
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(row)
    result = verify_approval(
        "aid-1", "prod-pg-1", "execute_sql",
        payload={"sql": "UPDATE t SET x=1 WHERE id=5"},
    )
    assert result == {"ok": True}


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_payload_mismatch_rejected(mock_boto3):
    """The headline P0: an approval for one SQL must not execute a different
    one on the same cluster/action_type."""
    row = _row_with_hash("execute_sql", {"sql": "UPDATE t SET x=1 WHERE id=5"})
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(row)
    result = verify_approval(
        "aid-1", "prod-pg-1", "execute_sql",
        payload={"sql": "UPDATE t SET x=1"},  # WHERE dropped
    )
    assert result["ok"] is False
    assert "does not match" in result["reason"]
    # A mismatch must NOT consume the row — the legit approval stays usable.
    mock_boto3.resource.return_value.Table.return_value.update_item.assert_not_called()


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_payload_bound_row_but_no_payload_passed_rejected(mock_boto3):
    """Fail-closed: a payload-bound row with a tool that forgot to pass the
    payload must be refused, not waved through."""
    row = _row_with_hash("execute_sql", {"sql": "SELECT pg_reload_conf()"})
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(row)
    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "payload-bound" in result["reason"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_legacy_row_without_hash_skips_binding(mock_boto3):
    """Rows minted before payload-binding shipped have no payload_hash — the
    guard must not break them across the deploy boundary."""
    mock_boto3.resource.return_value.Table.return_value = _scan_returning(_fresh_row())
    result = verify_approval(
        "aid-1", "prod-pg-1", "execute_sql", payload={"sql": "anything"}
    )
    assert result == {"ok": True}


def test_canonical_hash_request_and_execute_shapes_agree():
    """request_approval stores hash(action_details); the tool verifies with
    hash(its real args). These must agree for every write tool, including
    benign shape differences (parameter vs parameter_name, int vs float)."""
    # execute_sql: extra keys on the request side are ignored
    assert canonical_action_hash("execute_sql", {"sql": "VACUUM t", "note": "x"}) == \
        canonical_action_hash("execute_sql", {"sql": "VACUUM t"})
    # modify_parameter: response key "parameter" vs tool arg "parameter_name"
    assert canonical_action_hash("modify_parameter", {"parameter": "work_mem", "value": "64MB"}) == \
        canonical_action_hash("modify_parameter", {"parameter_name": "work_mem", "value": "64MB"})
    # modify_scaling: int vs float, and min_acu alias vs min_capacity
    assert canonical_action_hash("modify_scaling", {"min_acu": 2, "max_acu": 8}) == \
        canonical_action_hash("modify_scaling", {"min_capacity": 2.0, "max_capacity": 8.0})
    # different operations must NOT collide
    assert canonical_action_hash("execute_sql", {"sql": "DELETE FROM t"}) != \
        canonical_action_hash("execute_sql", {"sql": "DELETE FROM u"})
    # create_snapshot: the approval_required placeholder "(auto-generated)"
    # must hash the same as the empty arg the execute side passes.
    assert canonical_action_hash("create_snapshot", {"snapshot_id": "(auto-generated)"}) == \
        canonical_action_hash("create_snapshot", {"snapshot_id": ""})
    # ...but a NAMED snapshot must not collide with the auto path.
    assert canonical_action_hash("create_snapshot", {"snapshot_id": "nightly-1"}) != \
        canonical_action_hash("create_snapshot", {"snapshot_id": ""})


def test_other_bucket_preserves_string_distinctness():
    """The generic 'other' projection must NOT numeric-coerce, or distinct
    string payloads could collide into one approval."""
    assert canonical_action_hash("other", {"code": "001"}) != \
        canonical_action_hash("other", {"code": "1"})
    assert canonical_action_hash("other", {"a": 1, "b": 2}) == \
        canonical_action_hash("other", {"b": 2, "a": 1})  # key order irrelevant


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"})
@patch("mcp_servers.shared.approval_guard.boto3")
def test_empty_action_type_rejected_fail_closed(mock_boto3):
    """action_type이 빈 승인 행은 거부한다 — 비어 있으면 매칭이 스킵되어
    임의 쓰기 툴 승인으로 재사용될 수 있었다(Codex 감사)."""
    row = _fresh_row(action_type="")
    table = _scan_returning(row)
    mock_boto3.resource.return_value.Table.return_value = table

    result = verify_approval("aid-1", "prod-pg-1", "execute_sql")
    assert result["ok"] is False
    assert "action_type" in result["reason"]
    table.update_item.assert_not_called()
