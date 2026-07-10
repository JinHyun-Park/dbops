"""Tests for the restore_finalizer second pass (N-④ Phase 1): driving scale-out
prewarm approval rows through their state machine.

Covers: awaiting_instance + reader available → pending; awaiting_instance +
still creating → unchanged; approved → operations Lambda invoked with the right
payload + ClientContext and warm_dispatched set; vanished instance →
awaiting_instance_failed; rejected/consumed untouched; scan pagination hang-guard
against a bare MagicMock.
"""

import base64
import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import MagicMock

_HANDLER_PATH = (
    Path(__file__).resolve().parents[3]
    / "data-pipeline"
    / "restore_finalizer"
    / "handler.py"
)
_spec = importlib.util.spec_from_file_location("restore_finalizer_handler_so", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _row(status="awaiting_instance", **extra):
    row = {
        "approval_id": "aid-1",
        "created_at": "1700000000000",
        "cluster_id": "prod-pg-1",
        "reader_instance_id": "reader-2",
        "action_type": "prewarm_reader",
        "scaleout": True,
        "approval_status": status,
        "region": "ap-northeast-2",
        "spoke_role_arn": "arn:aws:iam::222:role/dbops-spoke",
        "action_details": {"cluster_id": "prod-pg-1", "reader_instance_id": "reader-2",
                           "endpoint_identifier": "ep-ro", "top_n": 20},
    }
    row.update(extra)
    return row


def _rds_with_status(status):
    rds = MagicMock()
    rds.describe_db_instances.return_value = {"DBInstances": [
        {"DBInstanceIdentifier": "reader-2", "DBInstanceStatus": status},
    ]}
    return rds


def test_available_reader_flips_to_pending(monkeypatch):
    rds = _rds_with_status("available")
    monkeypatch.setattr(handler, "_rds_for", lambda region="", role_arn="": rds)
    table = MagicMock()
    out = handler._advance_prewarm(table, "ops-fn", _row())
    assert out["result"] == "queued_pending"
    vals = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert vals[":status"] == "pending"


def test_still_creating_left_unchanged(monkeypatch):
    rds = _rds_with_status("creating")
    monkeypatch.setattr(handler, "_rds_for", lambda region="", role_arn="": rds)
    table = MagicMock()
    out = handler._advance_prewarm(table, "ops-fn", _row())
    assert out["result"].startswith("waiting")
    table.update_item.assert_not_called()


def test_vanished_instance_marks_failed(monkeypatch):
    rds = MagicMock()
    rds.describe_db_instances.side_effect = Exception("DBInstanceNotFoundFault: gone")
    monkeypatch.setattr(handler, "_rds_for", lambda region="", role_arn="": rds)
    table = MagicMock()
    out = handler._advance_prewarm(table, "ops-fn", _row())
    assert out["result"] == "instance_vanished"
    vals = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert vals[":status"] == "awaiting_instance_failed"


def test_approved_invokes_operations_lambda(monkeypatch):
    fake_boto3 = MagicMock()
    lambda_client = MagicMock()
    fake_boto3.client.return_value = lambda_client
    # Synchronous invoke returns a response whose Payload is the operations
    # handler's MCP envelope wrapping the prewarm_reader result.
    lambda_client.invoke.return_value = {
        "StatusCode": 200,
        "Payload": io.BytesIO(
            json.dumps({"content": [{"type": "text",
                        "text": json.dumps({"status": "prewarmed"})}]}).encode("utf-8")
        ),
    }
    monkeypatch.setattr(handler, "boto3", fake_boto3)
    # Stub the best-effort event_log so a leaked CACHE_DB_* env from another test
    # can't add a second boto3.client("rds-data") call and skew this assertion.
    monkeypatch.setattr(handler, "_event_log", lambda *a, **k: None)
    table = MagicMock()

    out = handler._advance_prewarm(table, "dbops-dev-operations-mcp", _row(status="approved"))
    assert out["result"] == "warm_dispatched"
    assert out["warm_status"] == "prewarmed"
    assert out["warm_result"] == "prewarmed"

    # The lambda client is built with an explicit botocore Config (read_timeout
    # > operations Lambda 120s, no auto-retry) so the sync invoke can't time out
    # mid-run and double-fire.
    assert fake_boto3.client.call_args.args == ("lambda",)
    assert "config" in fake_boto3.client.call_args.kwargs
    kw = lambda_client.invoke.call_args.kwargs
    assert kw["FunctionName"] == "dbops-dev-operations-mcp"
    # RequestResponse (NOT async Event): Lambda only delivers ClientContext —
    # carrying the tool name — on synchronous invokes.
    assert kw["InvocationType"] == "RequestResponse"
    # ClientContext decodes to the tool_name the operations handler reads.
    ctx = json.loads(base64.b64decode(kw["ClientContext"]))
    assert ctx["custom"]["tool_name"] == "prewarm_reader"
    # Payload carries the hash-bound endpoint_identifier + top_n + approval_id.
    payload = json.loads(kw["Payload"])
    assert payload == {
        "cluster_id": "prod-pg-1", "reader_instance_id": "reader-2",
        "endpoint_identifier": "ep-ro", "top_n": 20,
        "approved": True, "approval_id": "aid-1",
    }
    # warm_dispatched marker set so the next tick doesn't double-invoke, and the
    # actual outcome recorded as warm_result.
    vals = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert vals[":warm_dispatched"] is True
    assert vals[":warm_result"] == "prewarmed"


def test_approved_failed_warm_records_warm_result_failed(monkeypatch):
    """A prewarm that RAN but failed (non-prewarmed status / FunctionError) must
    still set warm_dispatched=True (a deterministic failure won't fix on retry,
    and a post-verify failure already consumed the approval) AND record
    warm_result="failed" so the UI shows a terminal warm_failed, not warming."""
    fake_boto3 = MagicMock()
    lambda_client = MagicMock()
    fake_boto3.client.return_value = lambda_client
    lambda_client.invoke.return_value = {
        "StatusCode": 200,
        "Payload": io.BytesIO(
            json.dumps({"content": [{"type": "text",
                        "text": json.dumps({"status": "connect_failed"})}]}).encode("utf-8")
        ),
    }
    monkeypatch.setattr(handler, "boto3", fake_boto3)
    monkeypatch.setattr(handler, "_event_log", lambda *a, **k: None)
    table = MagicMock()

    out = handler._advance_prewarm(table, "ops-fn", _row(status="approved"))
    assert out["result"] == "warm_dispatched"
    assert out["warm_result"] == "failed"
    vals = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert vals[":warm_dispatched"] is True
    assert vals[":warm_result"] == "failed"


def test_already_dispatched_is_skipped(monkeypatch):
    fake_boto3 = MagicMock()
    monkeypatch.setattr(handler, "boto3", fake_boto3)
    table = MagicMock()
    out = handler._advance_prewarm(table, "ops-fn", _row(status="approved", warm_dispatched=True))
    assert out["result"] == "already_dispatched"
    fake_boto3.client.assert_not_called()
    table.update_item.assert_not_called()


def test_rejected_and_consumed_untouched():
    table = MagicMock()
    for status in ("rejected", "consumed"):
        out = handler._advance_prewarm(table, "ops-fn", _row(status=status))
        assert out["result"] == f"skip:{status}"
    table.update_item.assert_not_called()


def test_scan_pagination_hang_guard():
    """A bare MagicMock table must NOT drive an infinite scan loop: its
    LastEvaluatedKey isn't a dict, so the paginator stops immediately."""
    table = MagicMock()  # .scan() returns a MagicMock; .get(...) returns MagicMocks
    out = handler._scan_scaleout_prewarms(table)
    assert out == []  # non-list Items skipped, non-dict LEK → break


def test_scan_paginates_real_pages():
    table = MagicMock()
    table.scan.side_effect = [
        {"Items": [_row()], "LastEvaluatedKey": {"approval_id": "aid-1"}},
        {"Items": [_row(approval_id="aid-2")]},
    ]
    out = handler._scan_scaleout_prewarms(table)
    assert [r["approval_id"] for r in out] == ["aid-1", "aid-2"]
