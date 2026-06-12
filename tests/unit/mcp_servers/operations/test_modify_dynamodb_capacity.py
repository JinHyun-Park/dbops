"""Tests for modify_dynamodb_capacity — the 3-state approval flow plus the
safety-fix regressions: <1 rejection (#4), GSI block (#5), TOCTOU drift (#6),
table-target binding into the hash (#1).

verify_approval is bypassed via APPROVAL_GUARD_BYPASS for the success paths
(guard logic is tested separately in test_approval_guard.py); client_for_cluster
is mocked so no AWS call is ever made.
"""

from unittest.mock import MagicMock, patch

import pytest
from mcp_servers.operations.tools.modify_dynamodb_capacity import (
    modify_dynamodb_capacity_impl,
)


@pytest.fixture(autouse=True)
def _mock_table_name():
    """The real table name comes from the cluster registry (table_name_for_cluster),
    NOT cluster_meta — pin it to 'orders' so the target-binding assertions exercise
    the real resolution path."""
    with patch(
        "mcp_servers.operations.tools.modify_dynamodb_capacity.table_name_for_cluster",
        return_value="orders",
    ):
        yield


def _cache_with_name(name="orders"):
    return MagicMock()


def _ddb_client(*, billing_mode="PROVISIONED", rcu=5, wcu=5, gsis=None, describe_seq=None):
    """A mock DynamoDB client. describe_table returns one state (or a sequence
    for TOCTOU tests where request-time and execute-time differ)."""
    client = MagicMock()

    def _table(mode, r, w, g):
        return {
            "Table": {
                "BillingModeSummary": {"BillingMode": mode},
                "ProvisionedThroughput": {
                    "ReadCapacityUnits": r,
                    "WriteCapacityUnits": w,
                },
                "GlobalSecondaryIndexes": [
                    {"IndexName": n} for n in (g or [])
                ],
            }
        }

    if describe_seq is not None:
        client.describe_table.side_effect = [
            _table(m, r, w, g) for (m, r, w, g) in describe_seq
        ]
    else:
        client.describe_table.return_value = _table(billing_mode, rcu, wcu, gsis)
    return client


# ===== 3-state flow =====


@patch("mcp_servers.operations.tools.modify_dynamodb_capacity.client_for_cluster")
def test_capacity_requires_approval(mock_client_for):
    mock_client_for.return_value = _ddb_client(billing_mode="PROVISIONED", rcu=5, wcu=5)
    result = modify_dynamodb_capacity_impl(
        _cache_with_name(), cluster_id="ddb-abc", rcu=20, wcu=10
    )
    assert result["status"] == "approval_required"
    assert result["rcu"] == 20 and result["wcu"] == 10
    assert result["target"] == "orders"


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_dynamodb_capacity.client_for_cluster")
def test_capacity_approved_executes(mock_client_for):
    client = _ddb_client(billing_mode="PROVISIONED", rcu=5, wcu=5)
    mock_client_for.return_value = client
    result = modify_dynamodb_capacity_impl(
        _cache_with_name(), cluster_id="ddb-abc", rcu=20, wcu=10, approved=True
    )
    assert result["status"] == "modified"
    assert result["rcu"] == 20 and result["wcu"] == 10
    client.update_table.assert_called_once()
    kwargs = client.update_table.call_args.kwargs
    assert kwargs["ProvisionedThroughput"] == {
        "ReadCapacityUnits": 20,
        "WriteCapacityUnits": 10,
    }


@patch("mcp_servers.operations.tools.modify_dynamodb_capacity.client_for_cluster")
def test_capacity_approved_without_id_denied(mock_client_for):
    client = _ddb_client(billing_mode="PROVISIONED", rcu=5, wcu=5)
    mock_client_for.return_value = client
    with patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"}, clear=True):
        result = modify_dynamodb_capacity_impl(
            _cache_with_name(), cluster_id="ddb-abc", rcu=20, wcu=10, approved=True
        )
    assert result["status"] == "approval_denied"
    client.update_table.assert_not_called()


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_dynamodb_capacity.client_for_cluster")
def test_capacity_switch_to_ondemand_drops_capacity(mock_client_for):
    client = _ddb_client(billing_mode="PROVISIONED", rcu=5, wcu=5)
    mock_client_for.return_value = client
    result = modify_dynamodb_capacity_impl(
        _cache_with_name(), cluster_id="ddb-abc", billing_mode="On-Demand", approved=True
    )
    assert result["status"] == "modified"
    kwargs = client.update_table.call_args.kwargs
    assert kwargs["BillingMode"] == "PAY_PER_REQUEST"
    assert "ProvisionedThroughput" not in kwargs


@patch("mcp_servers.operations.tools.modify_dynamodb_capacity.client_for_cluster")
def test_capacity_switch_to_provisioned_requires_rcu_wcu(mock_client_for):
    mock_client_for.return_value = _ddb_client(billing_mode="PAY_PER_REQUEST", rcu=0, wcu=0)
    result = modify_dynamodb_capacity_impl(
        _cache_with_name(), cluster_id="ddb-abc", billing_mode="PROVISIONED"
    )
    assert result["status"] == "error"
    assert "RCU" in result["reason"]


# ===== safety-fix regressions =====


@patch("mcp_servers.operations.tools.modify_dynamodb_capacity.client_for_cluster")
def test_capacity_rejects_below_one_rcu(mock_client_for):
    """fix #4: rcu/wcu < 1 → error, NOT silently floored, and never executes."""
    client = _ddb_client(billing_mode="PROVISIONED", rcu=5, wcu=5)
    mock_client_for.return_value = client
    result = modify_dynamodb_capacity_impl(
        _cache_with_name(), cluster_id="ddb-abc", rcu=0, wcu=5, approved=True
    )
    assert result["status"] == "error"
    assert "1" in result["reason"]
    client.update_table.assert_not_called()


@patch("mcp_servers.operations.tools.modify_dynamodb_capacity.client_for_cluster")
def test_capacity_blocks_table_with_gsi(mock_client_for):
    """fix #5: a table with any GSI → unsupported on a capacity change."""
    client = _ddb_client(billing_mode="PROVISIONED", rcu=5, wcu=5, gsis=["byEmail"])
    mock_client_for.return_value = client
    result = modify_dynamodb_capacity_impl(
        _cache_with_name(), cluster_id="ddb-abc", rcu=20, wcu=10
    )
    assert result["status"] == "unsupported"
    assert "GSI" in result["reason"]
    client.update_table.assert_not_called()


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_dynamodb_capacity.client_for_cluster")
def test_capacity_toctou_drift_denied(mock_client_for):
    """fix #6: execute-time re-read shows the billing mode already switched →
    approval_denied, update_table never called."""
    # request-time: PROVISIONED 5/5; execute-time re-read: now PAY_PER_REQUEST.
    client = _ddb_client(
        describe_seq=[
            ("PROVISIONED", 5, 5, []),
            ("PAY_PER_REQUEST", 0, 0, []),
        ]
    )
    mock_client_for.return_value = client
    result = modify_dynamodb_capacity_impl(
        _cache_with_name(), cluster_id="ddb-abc", rcu=20, wcu=10, approved=True
    )
    assert result["status"] == "approval_denied"
    assert "changed since approval" in result["reason"]
    client.update_table.assert_not_called()


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_dynamodb_capacity.client_for_cluster")
def test_capacity_toctou_inplace_capacity_drift_denied(mock_client_for):
    """fix #6: in-place capacity change where the table's current capacity drifted
    between request and execute → denied."""
    client = _ddb_client(
        describe_seq=[
            ("PROVISIONED", 5, 5, []),
            ("PROVISIONED", 8, 8, []),  # drifted
        ]
    )
    mock_client_for.return_value = client
    result = modify_dynamodb_capacity_impl(
        _cache_with_name(), cluster_id="ddb-abc", rcu=20, wcu=10, approved=True
    )
    assert result["status"] == "approval_denied"
    client.update_table.assert_not_called()


@patch("mcp_servers.operations.tools.modify_dynamodb_capacity.client_for_cluster")
def test_capacity_describe_failure_is_error_not_raise(mock_client_for):
    """Never raises: a describe failure degrades to {status: error}."""
    client = MagicMock()
    client.describe_table.side_effect = Exception("boom")
    mock_client_for.return_value = client
    result = modify_dynamodb_capacity_impl(
        _cache_with_name(), cluster_id="ddb-abc", rcu=20, wcu=10, approved=True
    )
    assert result["status"] == "error"
    client.update_table.assert_not_called()


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_dynamodb_capacity.client_for_cluster")
def test_capacity_limit_exceeded_clean_error(mock_client_for):
    """A LimitExceededException from update_table → clean error, never crash."""
    from botocore.exceptions import ClientError

    client = _ddb_client(billing_mode="PROVISIONED", rcu=5, wcu=5)
    client.update_table.side_effect = ClientError(
        {"Error": {"Code": "LimitExceededException", "Message": "rate"}}, "UpdateTable"
    )
    mock_client_for.return_value = client
    result = modify_dynamodb_capacity_impl(
        _cache_with_name(), cluster_id="ddb-abc", rcu=20, wcu=10, approved=True
    )
    assert result["status"] == "error"
    assert "LimitExceededException" in result["reason"]
