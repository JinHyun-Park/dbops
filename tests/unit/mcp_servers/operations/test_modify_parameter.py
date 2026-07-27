from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.modify_parameter import modify_parameter_impl


def test_modify_parameter_requires_approval():
    """No approved=True → always returns approval_required, no RDS call."""
    mock_cache = MagicMock()
    result = modify_parameter_impl(
        mock_cache, cluster_id="prod-pg-1", parameter_name="max_connections", value="200"
    )
    assert result["status"] == "approval_required"
    assert result["parameter"] == "max_connections"
    assert result["value"] == "200"


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_modify_parameter_with_approval(mock_rds_for):
    """Approved + cluster on a CUSTOM parameter group → impl applies the
    change via modify_db_cluster_parameter_group. Guard bypassed via env."""
    mock_rds = MagicMock()
    mock_rds_for.return_value = mock_rds
    # The impl looks up the cluster's parameter group via DescribeDBClusters
    # before mutating — stub that with a non-default group name so the
    # default-group safety check passes.
    mock_rds.describe_db_clusters.return_value = {
        "DBClusters": [{"DBClusterParameterGroup": "prod-pg-1-custom-pg15"}],
    }
    mock_cache = MagicMock()
    result = modify_parameter_impl(
        mock_cache, cluster_id="prod-pg-1", parameter_name="max_connections", value="200", approved=True,
    )
    assert result["status"] == "modified"
    assert result["parameter"] == "max_connections"
    assert result["parameter_group"] == "prod-pg-1-custom-pg15"
    mock_rds.modify_db_cluster_parameter_group.assert_called_once()
    call_kwargs = mock_rds.modify_db_cluster_parameter_group.call_args.kwargs
    assert call_kwargs["DBClusterParameterGroupName"] == "prod-pg-1-custom-pg15"
    assert call_kwargs["Parameters"][0]["ParameterName"] == "max_connections"
    assert call_kwargs["Parameters"][0]["ParameterValue"] == "200"
    # pending-reboot 안내가 결과에 포함돼야 한다 — 에이전트가 "재시작 후
    # 적용"을 사용자에게 전달하게 하는 핵심(승인 후 즉시 반영 오해 방지).
    assert call_kwargs["Parameters"][0]["ApplyMethod"] == "pending-reboot"
    assert result["apply_method"] == "pending-reboot"
    assert result["applied"] is False
    assert "재시작" in result["note"]


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_modify_parameter_refuses_default_group(mock_rds_for):
    """Approved + cluster on the AWS-default parameter group → impl refuses
    and returns default_group_refused without calling modify."""
    mock_rds = MagicMock()
    mock_rds_for.return_value = mock_rds
    mock_rds.describe_db_clusters.return_value = {
        "DBClusters": [{"DBClusterParameterGroup": "default.aurora-postgresql15"}],
    }
    mock_cache = MagicMock()
    result = modify_parameter_impl(
        mock_cache, cluster_id="prod-pg-1", parameter_name="max_connections", value="200", approved=True,
    )
    assert result["status"] == "default_group_refused"
    assert result["parameter_group"].startswith("default.")
    mock_rds.modify_db_cluster_parameter_group.assert_not_called()


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_lookup_failure_returns_static_reason_no_exception_text(mock_rds_for):
    """describe_db_clusters blowing up must NOT put the exception into the
    response (project hard rule): static Korean reason + module logger only."""
    mock_rds = MagicMock()
    mock_rds_for.return_value = mock_rds
    mock_rds.describe_db_clusters.side_effect = RuntimeError(
        "AccessDenied boom: arn:aws:iam::123456789012:role/secret-ish"
    )
    result = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name="work_mem", value="64MB",
        approved=True,
    )
    assert result["status"] == "lookup_failed"
    assert "error" not in result
    blob = " ".join(str(v) for v in result.values())
    for leak in ("boom", "AccessDenied", "arn:aws:iam", "RuntimeError"):
        assert leak not in blob, f"raw exception text leaked: {result}"
    mock_rds.modify_db_cluster_parameter_group.assert_not_called()


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_modify_failure_returns_static_reason_no_exception_text(mock_rds_for):
    mock_rds = MagicMock()
    mock_rds_for.return_value = mock_rds
    mock_rds.describe_db_clusters.return_value = {
        "DBClusters": [{"DBClusterParameterGroup": "prod-pg-1-custom-pg15"}],
    }
    mock_rds.modify_db_cluster_parameter_group.side_effect = RuntimeError(
        "InvalidParameterValue boom: internal-detail-42"
    )
    result = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name="work_mem", value="64MB",
        approved=True,
    )
    assert result["status"] == "modify_failed"
    assert "error" not in result
    blob = " ".join(str(v) for v in result.values())
    for leak in ("boom", "InvalidParameterValue", "internal-detail-42"):
        assert leak not in blob, f"raw exception text leaked: {result}"


def test_modify_parameter_approved_without_id_rejected():
    """Bare `approved=True` (no approval_id) is rejected by the guard."""
    with patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"}, clear=True):
        mock_cache = MagicMock()
        result = modify_parameter_impl(
            mock_cache,
            cluster_id="prod-pg-1",
            parameter_name="max_connections",
            value="200",
            approved=True,
        )
        assert result["status"] == "approval_denied"
        assert "approval_id missing" in result["reason"]
