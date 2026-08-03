"""Tests for the Aurora custom-endpoint write tools (P2-⑤), approval-gated.

Covers: deny-by-default, payload-hash binding (real guard vs a mocked DDB row),
built-in writer/reader protection, member/type validation, and cli_preview
content. The engine-gate (unsupported_engine on non-relational) is exercised in
test_operations_engine_gate.py where the handler env is already set up.
"""

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.create_custom_endpoint import create_custom_endpoint_impl
from mcp_servers.operations.tools.delete_custom_endpoint import delete_custom_endpoint_impl
from mcp_servers.operations.tools.modify_custom_endpoint import modify_custom_endpoint_impl
from mcp_servers.shared.approval_guard import canonical_action_hash

MODULE = "mcp_servers.operations.tools.create_custom_endpoint"

_CE = "mcp_servers.operations.tools.create_custom_endpoint"
_DE = "mcp_servers.operations.tools.delete_custom_endpoint"
_ME = "mcp_servers.operations.tools.modify_custom_endpoint"

# A realistic AWS error: it carries the hub account id, the platform role name
# and the target ARN. Every one of those must stay in CloudWatch and NEVER reach
# a tool response the DBA reads in the agent transcript.
_LEAKY = (
    "An error occurred (AccessDenied) when calling the CreateDBClusterEndpoint "
    "operation: User: arn:aws:sts::123456789012:assumed-role/dbops-dev-operations-role/"
    "dbops-dev-operations is not authorized to perform: rds:CreateDBClusterEndpoint "
    "on resource: arn:aws:rds:ap-northeast-2:123456789012:cluster:prod-pg-1"
)
_LEAK_TOKENS = ("123456789012", "dbops-dev-operations-role", "AccessDenied", "arn:aws")


def _assert_no_leak(out):
    """No RESPONSE field may carry exception text (project-wide rule)."""
    blob = json.dumps(out, ensure_ascii=False, default=str)
    for token in _LEAK_TOKENS:
        assert token not in blob, f"exception text leaked into response: {token!r} in {blob!r}"
    assert "error" not in out, "raw exception field must be gone"


def _rds_with_members(ids):
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {
        "DBClusters": [{"DBClusterMembers": [{"DBInstanceIdentifier": i} for i in ids]}]
    }
    rds.create_db_cluster_endpoint.return_value = {"Status": "creating", "Endpoint": "ep.x.rds.amazonaws.com"}
    return rds


def _rds_with_endpoint(endpoint_type="CUSTOM", **extra):
    rds = MagicMock()
    ep = {"DBClusterEndpointIdentifier": "ep-analytics", "EndpointType": endpoint_type,
          "Status": "available", **extra}
    rds.describe_db_cluster_endpoints.return_value = {"DBClusterEndpoints": [ep]}
    rds.delete_db_cluster_endpoint.return_value = {"Status": "deleting"}
    rds.modify_db_cluster_endpoint.return_value = {"Status": "modifying"}
    return rds


# ───────────────────────── create ─────────────────────────

@patch(f"{_CE}.rds_client_for_cluster")
def test_create_requires_approval(mock_rds_for):
    mock_rds_for.return_value = _rds_with_members(["i-1", "i-2"])
    out = create_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-analytics",
        endpoint_type="READER", static_members=["i-1"],
    )
    assert out["status"] == "approval_required"
    assert out["cli_preview"] == (
        "aws rds create-db-cluster-endpoint --db-cluster-identifier prod-pg-1 "
        "--db-cluster-endpoint-identifier ep-analytics --endpoint-type READER "
        "--static-members i-1"
    )


@patch(f"{_CE}.rds_client_for_cluster")
def test_create_rejects_bad_type(mock_rds_for):
    mock_rds_for.return_value = _rds_with_members(["i-1"])
    out = create_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-x", endpoint_type="WRITER",
    )
    assert out["status"] == "invalid_endpoint_type"


@patch(f"{_CE}.rds_client_for_cluster")
def test_create_rejects_non_member(mock_rds_for):
    mock_rds_for.return_value = _rds_with_members(["i-1", "i-2"])
    out = create_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-x",
        endpoint_type="READER", static_members=["i-9"],
    )
    assert out["status"] == "invalid_members"
    assert "i-9" in out["reason"]


@patch(f"{_CE}.rds_client_for_cluster")
def test_create_rejects_mutually_exclusive_members(mock_rds_for):
    mock_rds_for.return_value = _rds_with_members(["i-1", "i-2"])
    out = create_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-x",
        endpoint_type="ANY", static_members=["i-1"], excluded_members=["i-2"],
    )
    assert out["status"] == "invalid_members"


@patch(f"{_CE}.verify_approval")
@patch(f"{_CE}.rds_client_for_cluster")
def test_create_executes_when_approved(mock_rds_for, mock_guard):
    rds = _rds_with_members(["i-1", "i-2"])
    mock_rds_for.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = create_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-analytics",
        endpoint_type="READER", static_members=["i-1"], approved=True, approval_id="aid-1",
    )
    assert out["status"] == "creating"
    call = rds.create_db_cluster_endpoint.call_args.kwargs
    assert call["DBClusterEndpointIdentifier"] == "ep-analytics"
    assert call["EndpointType"] == "READER"
    assert call["StaticMembers"] == ["i-1"]


@patch(f"{_CE}.rds_client_for_cluster")
def test_create_payload_hash_mismatch_rejected(mock_rds_for):
    """A real approval minted for members [i-1] cannot be consumed to create the
    same endpoint with members [i-2] — the guard's payload_hash refuses it."""
    mock_rds_for.return_value = _rds_with_members(["i-1", "i-2"])
    row = {
        "approval_id": "aid-1", "created_at": "1", "approval_status": "approved",
        "cluster_id": "prod-pg-1", "action_type": "create_custom_endpoint",
        "payload_hash": canonical_action_hash("create_custom_endpoint", {
            "endpoint_identifier": "ep-analytics", "endpoint_type": "READER",
            "static_members": ["i-1"], "excluded_members": [],
        }),
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    table = MagicMock()
    table.scan.return_value = {"Items": [row]}
    resource = MagicMock()
    resource.Table.return_value = table
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}, clear=False), \
         patch("mcp_servers.shared.approval_guard.boto3.resource", return_value=resource):
        out = create_custom_endpoint_impl(
            MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-analytics",
            endpoint_type="READER", static_members=["i-2"], approved=True, approval_id="aid-1",
        )
    assert out["status"] == "approval_denied"
    assert "match" in out["reason"].lower()
    table.update_item.assert_not_called()  # never consumed


def test_create_projection_is_member_order_independent():
    a = canonical_action_hash("create_custom_endpoint", {
        "endpoint_identifier": "ep", "endpoint_type": "READER",
        "static_members": ["i-2", "i-1"], "excluded_members": [],
    })
    b = canonical_action_hash("create_custom_endpoint", {
        "endpoint_identifier": "ep", "endpoint_type": "READER",
        "static_members": ["i-1", "i-2"], "excluded_members": [],
    })
    assert a == b


# ───────────────────────── delete ─────────────────────────

@patch(f"{_DE}.rds_client_for_cluster")
def test_delete_requires_approval(mock_rds_for):
    mock_rds_for.return_value = _rds_with_endpoint("CUSTOM")
    out = delete_custom_endpoint_impl(MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-analytics")
    assert out["status"] == "approval_required"
    assert out["cli_preview"] == (
        "aws rds delete-db-cluster-endpoint --db-cluster-endpoint-identifier ep-analytics"
    )


@patch(f"{_DE}.rds_client_for_cluster")
def test_delete_protects_builtin(mock_rds_for):
    """A WRITER/READER built-in endpoint can NEVER be deleted through the tool."""
    rds = _rds_with_endpoint("WRITER")
    mock_rds_for.return_value = rds
    out = delete_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-analytics",
        approved=True, approval_id="aid-1",
    )
    assert out["status"] == "builtin_protected"
    rds.delete_db_cluster_endpoint.assert_not_called()


@patch(f"{_DE}.rds_client_for_cluster")
def test_delete_not_found(mock_rds_for):
    rds = MagicMock()
    rds.describe_db_cluster_endpoints.return_value = {"DBClusterEndpoints": []}
    mock_rds_for.return_value = rds
    out = delete_custom_endpoint_impl(MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ghost")
    assert out["status"] == "not_found"


@patch(f"{_DE}.verify_approval")
@patch(f"{_DE}.rds_client_for_cluster")
def test_delete_executes_when_approved(mock_rds_for, mock_guard):
    rds = _rds_with_endpoint("CUSTOM")
    mock_rds_for.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = delete_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-analytics",
        approved=True, approval_id="aid-1",
    )
    assert out["status"] == "deleting"
    rds.delete_db_cluster_endpoint.assert_called_once_with(DBClusterEndpointIdentifier="ep-analytics")


# ───────────────────────── modify ─────────────────────────

@patch(f"{_ME}.rds_client_for_cluster")
def test_modify_requires_members(mock_rds_for):
    out = modify_custom_endpoint_impl(MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-analytics")
    assert out["status"] == "nothing_to_modify"


@patch(f"{_ME}.rds_client_for_cluster")
def test_modify_requires_approval(mock_rds_for):
    mock_rds_for.return_value = _rds_with_endpoint("CUSTOM")
    out = modify_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-analytics", static_members=["i-1"],
    )
    assert out["status"] == "approval_required"
    assert "modify-db-cluster-endpoint" in out["cli_preview"]
    assert "--static-members i-1" in out["cli_preview"]


@patch(f"{_ME}.rds_client_for_cluster")
def test_modify_protects_builtin(mock_rds_for):
    rds = _rds_with_endpoint("READER")
    mock_rds_for.return_value = rds
    out = modify_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-analytics",
        static_members=["i-1"], approved=True, approval_id="aid-1",
    )
    assert out["status"] == "builtin_protected"
    rds.modify_db_cluster_endpoint.assert_not_called()


@patch(f"{_ME}.verify_approval")
@patch(f"{_ME}.rds_client_for_cluster")
def test_modify_executes_when_approved(mock_rds_for, mock_guard):
    rds = _rds_with_endpoint("CUSTOM")
    mock_rds_for.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = modify_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-analytics",
        excluded_members=["i-3"], approved=True, approval_id="aid-1",
    )
    assert out["status"] == "modifying"
    call = rds.modify_db_cluster_endpoint.call_args.kwargs
    assert call["DBClusterEndpointIdentifier"] == "ep-analytics"
    assert call["ExcludedMembers"] == ["i-3"]


# ──────────────── no exception text in any response field ────────────────

@patch(f"{_CE}.rds_client_for_cluster")
def test_create_describe_failure_leaks_no_exception(mock_rds_for):
    rds = MagicMock()
    rds.describe_db_clusters.side_effect = RuntimeError(_LEAKY)
    mock_rds_for.return_value = rds
    out = create_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-x",
        endpoint_type="READER",
    )
    assert out["status"] == "error"
    _assert_no_leak(out)
    assert "cli_preview" in out  # built from known inputs, kept


@patch(f"{_CE}.verify_approval")
@patch(f"{_CE}.rds_client_for_cluster")
def test_create_failure_leaks_no_exception(mock_rds_for, mock_guard):
    rds = _rds_with_members(["i-1"])
    rds.create_db_cluster_endpoint.side_effect = RuntimeError(_LEAKY)
    mock_rds_for.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = create_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-analytics",
        endpoint_type="READER", static_members=["i-1"], approved=True, approval_id="aid-1",
    )
    assert out["status"] == "create_failed"
    _assert_no_leak(out)
    assert out["endpoint_identifier"] == "ep-analytics"
    assert out["cli_preview"].startswith("aws rds create-db-cluster-endpoint")


@patch(f"{_ME}.verify_approval")
@patch(f"{_ME}.rds_client_for_cluster")
def test_modify_failure_leaks_no_exception(mock_rds_for, mock_guard):
    rds = _rds_with_endpoint("CUSTOM")
    rds.modify_db_cluster_endpoint.side_effect = RuntimeError(_LEAKY)
    mock_rds_for.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = modify_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-analytics",
        excluded_members=["i-3"], approved=True, approval_id="aid-1",
    )
    assert out["status"] == "modify_failed"
    _assert_no_leak(out)
    assert out["cli_preview"].startswith("aws rds modify-db-cluster-endpoint")


@patch(f"{_DE}.verify_approval")
@patch(f"{_DE}.rds_client_for_cluster")
def test_delete_failure_leaks_no_exception(mock_rds_for, mock_guard):
    rds = _rds_with_endpoint("CUSTOM")
    rds.delete_db_cluster_endpoint.side_effect = RuntimeError(_LEAKY)
    mock_rds_for.return_value = rds
    mock_guard.return_value = {"ok": True}
    out = delete_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-analytics",
        approved=True, approval_id="aid-1",
    )
    assert out["status"] == "delete_failed"
    _assert_no_leak(out)
    assert out["cli_preview"].startswith("aws rds delete-db-cluster-endpoint")


@patch(f"{_DE}.rds_client_for_cluster")
def test_lookup_failure_leaks_no_exception(mock_rds_for):
    """find_custom_endpoint is shared by delete + modify: the describe failure
    reason must be static too."""
    rds = MagicMock()
    rds.describe_db_cluster_endpoints.side_effect = RuntimeError(_LEAKY)
    mock_rds_for.return_value = rds
    out = delete_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ep-analytics",
    )
    assert out["status"] == "error"
    _assert_no_leak(out)


@patch(f"{_DE}.rds_client_for_cluster")
def test_lookup_notfound_classification_survives(mock_rds_for):
    """str(e) still classifies NotFound (control flow unchanged) while the
    response stays free of exception text."""
    rds = MagicMock()
    rds.describe_db_cluster_endpoints.side_effect = RuntimeError(
        "EndpointNotFoundFault: arn:aws:rds:ap-northeast-2:123456789012:cluster-endpoint:ghost"
    )
    mock_rds_for.return_value = rds
    out = delete_custom_endpoint_impl(
        MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="ghost",
    )
    assert out["status"] == "not_found"
    _assert_no_leak(out)
    assert "ghost" in out["reason"]  # caller-supplied identifier, safe to echo


def test_describe_failure_on_the_demo_row_does_not_blame_the_cluster_id():
    """"cluster_id를 확인하세요" is wrong for the sample row: the id is correct.

    The row is registered and deliberately has no live AWS cluster, so the describe
    always fails and the operator was told to check an id that is right.
    """
    with patch(f"{MODULE}.rds_client_for_cluster") as mock_client, \
         patch(f"{MODULE}.demo_cluster_note", return_value="이 클러스터는 데모 행입니다."):
        mock_client.return_value.describe_db_clusters.side_effect = Exception("not found")
        result = create_custom_endpoint_impl(
            MagicMock(), cluster_id="sample-cluster", endpoint_identifier="analytics",
            static_members=["i-1"],
        )

    assert result["status"] == "error"
    assert "cluster_id를 확인하세요" not in result["reason"]
    assert "데모" in result["reason"]


def test_describe_failure_on_a_real_cluster_keeps_the_original_reason():
    """Negative control: a real cluster whose describe fails still gets the
    check-the-id advice, which is the right advice there."""
    with patch(f"{MODULE}.rds_client_for_cluster") as mock_client, \
         patch(f"{MODULE}.demo_cluster_note", return_value=""):
        mock_client.return_value.describe_db_clusters.side_effect = Exception("boom")
        result = create_custom_endpoint_impl(
            MagicMock(), cluster_id="prod-pg-1", endpoint_identifier="analytics",
            static_members=["i-1"],
        )

    assert result["status"] == "error"
    assert "cluster_id를 확인하세요" in result["reason"]
