"""Tests for request_approval — verify all action_types including EC-4 are accepted."""
import importlib.util
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

_BASE = Path(__file__).resolve().parents[4] / "mcp-servers/mcp_servers/operations/tools"


def _load(name):
    p = _BASE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


request_approval = _load("request_approval")


def test_request_approval_accepts_execute_sql():
    """request_approval accepts execute_sql action_type."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cluster", action_type="execute_sql",
                action_details={"sql": "SELECT 1"}
            )
            assert result["status"] == "pending"
            assert result["action_type"] == "execute_sql"


def test_request_approval_accepts_modify_elasticache_node_type():
    """EC-4: request_approval accepts modify_elasticache_node_type."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cache", action_type="modify_elasticache_node_type",
                action_details={"node_type": "cache.r7g.large"}
            )
            assert result["status"] == "pending"
            assert result["action_type"] == "modify_elasticache_node_type"


def test_request_approval_accepts_create_elasticache_snapshot():
    """EC-4: request_approval accepts create_elasticache_snapshot."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cache", action_type="create_elasticache_snapshot",
                action_details={"snapshot_name": "snap1"}
            )
            assert result["status"] == "pending"
            assert result["action_type"] == "create_elasticache_snapshot"


def test_request_approval_accepts_reboot_elasticache():
    """EC-4: request_approval accepts reboot_elasticache."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cache", action_type="reboot_elasticache",
                action_details={}
            )
            assert result["status"] == "pending"
            assert result["action_type"] == "reboot_elasticache"


def test_request_approval_accepts_test_elasticache_failover():
    """EC-4: request_approval accepts test_elasticache_failover."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cache", action_type="test_elasticache_failover",
                action_details={}
            )
            assert result["status"] == "pending"
            assert result["action_type"] == "test_elasticache_failover"


def test_request_approval_rejects_unknown_action_type():
    """request_approval rejects unknown action_types as expected."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cluster", action_type="unknown_action_xyz",
                action_details={}
            )
            assert result["status"] == "error"
            assert "unknown action_type" in result["message"]


# ===== N-①: origin is stamped by the API, NEVER by this tool =================


def _put_item(action_type="create_custom_endpoint", details=None, **kw):
    """Call request_approval and return the DDB Item that was put."""
    details = details or {"endpoint_identifier": "ep1", "endpoint_type": "READER",
                          "static_members": [], "excluded_members": []}
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            table = MagicMock()
            mock_boto3.resource.return_value.Table.return_value = table
            result = request_approval.request_approval_impl(
                None, cluster_id="c1", action_type=action_type,
                action_details=details, **kw,
            )
    return table.put_item.call_args.kwargs["Item"], result


def test_tool_never_writes_origin():
    """Trust boundary: request_approval must NOT write an origin marker — the
    auto-execute gate keys on origin=="ui", and only the trusted approvals API
    Lambda stamps it (after this tool returns). If the tool wrote origin, the
    agent could mint a UI-looking row via the gateway and get a chat-initiated
    destructive write auto-executed."""
    item, _ = _put_item()
    assert "origin" not in item


def test_tool_rejects_origin_kwarg():
    """origin is not a parameter of this tool — passing it must raise, proving
    the agent has no channel to set it through the tool."""
    import pytest
    with pytest.raises(TypeError):
        _put_item(origin="ui")


def test_created_at_returned_for_api_stamp():
    """The API caller needs (approval_id, created_at) to address the row it just
    created and stamp origin, so both must be in the return."""
    item, result = _put_item()
    assert result["status"] == "pending"
    assert result["approval_id"] == item["approval_id"]
    assert result["created_at"] == item["created_at"]


# ===== E-0: no exception text in the response, profiler range on both paths ===


def test_put_item_failure_has_no_exception_text():
    """The DDB error message can carry the table ARN / account id / the whole
    item, and this string is shown in chat. Static reason, details to the log."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            table = MagicMock()
            table.put_item.side_effect = RuntimeError(
                "ValidationException: arn:aws:dynamodb:ap-northeast-2:123456789012:table/secret"
            )
            mock_boto3.resource.return_value.Table.return_value = table
            result = request_approval.request_approval_impl(
                None, cluster_id="c1", action_type="execute_sql",
                action_details={"sql": "SELECT 1"},
            )
    assert result["status"] == "error"
    blob = " ".join(str(v) for v in result.values())
    for leak in ("arn:aws", "123456789012", "ValidationException", "RuntimeError"):
        assert leak not in blob, f"raw exception text leaked: {result}"


def test_profiler_registration_rejects_out_of_range_threshold():
    """FINDING 3: this path mints the payload_hash the write is bound to, so a
    value set_docdb_profiler would refuse must never reach the Approval Center
    (the DBA would approve a change that then dead-ends at execute time)."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            table = MagicMock()
            mock_boto3.resource.return_value.Table.return_value = table
            result = request_approval.request_approval_impl(
                None, cluster_id="docdb-1", action_type="set_docdb_profiler",
                action_details={"enabled": True, "threshold_ms": 10, "sampling_rate": 1.0},
            )
    assert result["status"] == "error"
    assert "threshold_ms" in result["message"]
    table.put_item.assert_not_called()


def test_profiler_registration_rejects_out_of_range_sampling_rate():
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            table = MagicMock()
            mock_boto3.resource.return_value.Table.return_value = table
            result = request_approval.request_approval_impl(
                None, cluster_id="docdb-1", action_type="set_docdb_profiler",
                action_details={"enabled": True, "sampling_rate": 1.5},
            )
    assert result["status"] == "error"
    assert "sampling_rate" in result["message"]
    table.put_item.assert_not_called()


def test_profiler_registration_accepts_in_range_and_defaults():
    """Control: an in-range payload (and an omitted knob, which takes the tool
    default) still registers."""
    item, result = _put_item(
        "set_docdb_profiler", {"enabled": False, "threshold_ms": 500}
    )
    assert result["status"] == "pending"
    assert item["action_type"] == "set_docdb_profiler"
