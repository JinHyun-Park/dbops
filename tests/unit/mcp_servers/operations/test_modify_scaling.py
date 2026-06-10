from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.modify_scaling import modify_scaling_impl


def _rds_with_cluster(cluster: dict):
    """Mock RDS client whose describe_db_clusters returns `cluster`. The
    engine-mode guard now runs BEFORE the approval branch, so every path
    through the tool needs a describable cluster."""
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [cluster]}
    return rds


_SV2_CLUSTER = {
    "EngineMode": "provisioned",  # AWS reports Sv2 clusters as "provisioned" too
    "ServerlessV2ScalingConfiguration": {"MinCapacity": 0.5, "MaxCapacity": 2.0},
}
_PROVISIONED_CLUSTER = {"EngineMode": "provisioned"}


@patch("mcp_servers.operations.tools.modify_scaling.rds_client_for_cluster")
def test_modify_scaling_requires_approval(mock_rds_for):
    mock_rds_for.return_value = _rds_with_cluster(_SV2_CLUSTER)
    mock_cache = MagicMock()
    result = modify_scaling_impl(mock_cache, cluster_id="prod-pg-1", min_capacity=0.5, max_capacity=4.0)
    assert result["status"] == "approval_required"
    assert result["min_capacity"] == 0.5
    assert result["max_capacity"] == 4.0


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_scaling.rds_client_for_cluster")
def test_modify_scaling_with_approval(mock_rds_for):
    """With the guard bypassed via env, approved=True executes the modify."""
    mock_rds = _rds_with_cluster(_SV2_CLUSTER)
    mock_rds_for.return_value = mock_rds
    mock_cache = MagicMock()
    result = modify_scaling_impl(
        mock_cache, cluster_id="prod-pg-1", min_capacity=0.5, max_capacity=4.0, approved=True
    )
    assert result["status"] == "modified"
    assert result["scaling"] == {"MinCapacity": 0.5, "MaxCapacity": 4.0}
    mock_rds.modify_db_cluster.assert_called_once()


@patch("mcp_servers.operations.tools.modify_scaling.rds_client_for_cluster")
def test_modify_scaling_approved_without_id_rejected(mock_rds_for):
    """approved=True without approval_id is refused by the guard."""
    mock_rds_for.return_value = _rds_with_cluster(_SV2_CLUSTER)
    with patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"}, clear=True):
        mock_cache = MagicMock()
        result = modify_scaling_impl(
            mock_cache,
            cluster_id="prod-pg-1",
            min_capacity=0.5,
            max_capacity=4.0,
            approved=True,
        )
        assert result["status"] == "approval_denied"
        assert "approval_id missing" in result["reason"]


@patch("mcp_servers.operations.tools.modify_scaling.rds_client_for_cluster")
def test_modify_scaling_provisioned_refused_before_approval(mock_rds_for):
    """Provisioned cluster (no Sv2 scaling config): refuse with not_applicable
    BEFORE asking for approval — RDS would silently accept the Sv2 config and
    the tool would report a 'modified' that changes nothing, and the approval
    round-trip would burn a consumed approval on a no-op."""
    mock_rds = _rds_with_cluster(_PROVISIONED_CLUSTER)
    mock_rds_for.return_value = mock_rds
    mock_cache = MagicMock()
    result = modify_scaling_impl(mock_cache, cluster_id="pgtsd-1", min_capacity=0.5, max_capacity=4.0)
    assert result["status"] == "not_applicable"
    assert "프로비저닝" in result["reason"]
    mock_rds.modify_db_cluster.assert_not_called()


@patch("mcp_servers.operations.tools.modify_scaling.rds_client_for_cluster")
def test_modify_scaling_describe_failure_fails_closed(mock_rds_for):
    """If the engine mode cannot be determined, refuse rather than modify."""
    mock_rds = MagicMock()
    mock_rds.describe_db_clusters.side_effect = Exception("boom")
    mock_rds_for.return_value = mock_rds
    result = modify_scaling_impl(MagicMock(), cluster_id="x", min_capacity=1.0, approved=True)
    assert result["status"] == "error"
    mock_rds.modify_db_cluster.assert_not_called()
