from unittest.mock import MagicMock, patch

from mcp_servers.shared.models import QueryResult
from mcp_servers.simulation.tools.parameter_simulation import simulate_parameter_change_impl

MODULE = "mcp_servers.simulation.tools.parameter_simulation"


def _rds_with_params(pg_name: str, parameters: list[dict]) -> MagicMock:
    """Build a MagicMock RDS client whose describe_* calls return a custom
    parameter group and the given parameter rows (single, unpaginated page)."""
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [{"DBClusterParameterGroup": pg_name}]}
    rds.describe_db_cluster_parameters.return_value = {"Parameters": parameters}
    return rds


def test_simulate_dynamic_parameter_live():
    mock_cache = MagicMock()
    rds = _rds_with_params(
        "custom-pg15",
        [
            {
                "ParameterName": "work_mem",
                "ParameterValue": "4096",
                "ApplyType": "dynamic",
                "AllowedValues": "64-2147483647",
                "DataType": "integer",
                "IsModifiable": True,
                "Description": "Sets the maximum memory used for query workspaces.",
                "Source": "user",
            }
        ],
    )

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds):
        result = simulate_parameter_change_impl(
            mock_cache, cluster_id="prod-pg-1", parameter_name="work_mem", new_value="256000"
        )

    assert result["cluster_id"] == "prod-pg-1"
    assert result["parameter"] == "work_mem"
    assert result["current_value"] == "4096"
    assert result["new_value"] == "256000"
    assert result["is_dynamic"] is True
    assert result["requires_restart"] is False
    assert result["is_modifiable"] is True
    assert result["parameter_group"] == "custom-pg15"
    assert result["allowed_values"] == "64-2147483647"
    assert result["data_type"] == "integer"
    assert result["valid"] is True
    assert "live" in result["data_source"]
    assert "static fallback" not in result["data_source"]


def test_simulate_static_parameter_live_requires_restart():
    mock_cache = MagicMock()
    rds = _rds_with_params(
        "custom-pg15",
        [
            {
                "ParameterName": "shared_buffers",
                "ParameterValue": "1048576",
                "ApplyType": "static",
                "AllowedValues": "16-1073741823",
                "DataType": "integer",
                "IsModifiable": True,
                "Description": "Sets the number of shared memory buffers.",
                "Source": "user",
            }
        ],
    )

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds):
        result = simulate_parameter_change_impl(
            mock_cache, cluster_id="prod-pg-1", parameter_name="shared_buffers", new_value="2097152"
        )

    assert result["requires_restart"] is True
    assert result["is_dynamic"] is False
    assert "재시작" in result["recommendation"]
    assert "live" in result["data_source"]


def test_simulate_value_out_of_range_flagged_invalid():
    mock_cache = MagicMock()
    rds = _rds_with_params(
        "custom-pg15",
        [
            {
                "ParameterName": "max_connections",
                "ParameterValue": "100",
                "ApplyType": "static",
                "AllowedValues": "1-65535",
                "DataType": "integer",
                "IsModifiable": True,
                "Description": "Sets the maximum number of concurrent connections.",
                "Source": "user",
            }
        ],
    )

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds):
        result = simulate_parameter_change_impl(
            mock_cache, cluster_id="prod-pg-1", parameter_name="max_connections", new_value="99999999"
        )

    assert result["valid"] is False
    assert "validation_reason" in result
    # A failed validation must not crash — the simulation still returns advice.
    assert result["data_source"].startswith("live")


def test_simulate_non_modifiable_parameter():
    mock_cache = MagicMock()
    rds = _rds_with_params(
        "custom-pg15",
        [
            {
                "ParameterName": "server_version",
                "ParameterValue": "15.4",
                "ApplyType": "static",
                "AllowedValues": "",
                "DataType": "string",
                "IsModifiable": False,
                "Description": "Reports the server version.",
                "Source": "engine-default",
            }
        ],
    )

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds):
        result = simulate_parameter_change_impl(
            mock_cache, cluster_id="prod-pg-1", parameter_name="server_version", new_value="16.0"
        )

    assert result["is_modifiable"] is False
    assert "수정할 수 없" in result["recommendation"]


def test_simulate_empty_current_value_notes_engine_default():
    mock_cache = MagicMock()
    rds = _rds_with_params(
        "custom-pg15",
        [
            {
                "ParameterName": "work_mem",
                "ParameterValue": "",
                "ApplyType": "dynamic",
                "AllowedValues": "64-2147483647",
                "DataType": "integer",
                "IsModifiable": True,
                "Description": "Sets the maximum memory used for query workspaces.",
                "Source": "engine-default",
            }
        ],
    )

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds):
        result = simulate_parameter_change_impl(
            mock_cache, cluster_id="prod-pg-1", parameter_name="work_mem", new_value="256000"
        )

    assert result["current_value"] is None
    assert "current_value_note" in result


def test_simulate_paginates_describe_parameters():
    """The target parameter on a later page must still be found via Marker."""
    mock_cache = MagicMock()
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [{"DBClusterParameterGroup": "custom-pg15"}]}
    rds.describe_db_cluster_parameters.side_effect = [
        {"Parameters": [{"ParameterName": "autovacuum", "ApplyType": "dynamic"}], "Marker": "page2"},
        {
            "Parameters": [
                {
                    "ParameterName": "work_mem",
                    "ParameterValue": "4096",
                    "ApplyType": "dynamic",
                    "AllowedValues": "64-2147483647",
                    "DataType": "integer",
                    "IsModifiable": True,
                    "Description": "...",
                }
            ]
        },
    ]

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds):
        result = simulate_parameter_change_impl(
            mock_cache, cluster_id="prod-pg-1", parameter_name="work_mem", new_value="256000"
        )

    assert result["current_value"] == "4096"
    assert rds.describe_db_cluster_parameters.call_count == 2
    assert result["data_source"].startswith("live")


def test_simulate_static_fallback_on_describe_failure():
    """A cross-account/unreachable cluster (describe raises) falls back to the
    static PARAMETER_INFO heuristic and is labeled as such."""
    mock_cache = MagicMock()
    rds = MagicMock()
    rds.describe_db_clusters.side_effect = Exception("AccessDenied: assume-role failed")

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds):
        result = simulate_parameter_change_impl(
            mock_cache, cluster_id="spoke-cluster", parameter_name="work_mem", new_value="256MB"
        )

    # work_mem is dynamic per the static heuristic.
    assert result["is_dynamic"] is True
    assert result["requires_restart"] is False
    assert result["impact_area"] == "memory"
    assert "static fallback" in result["data_source"]


def test_simulate_static_fallback_on_default_parameter_group():
    """An AWS-default parameter group routes to the static heuristic path."""
    mock_cache = MagicMock()
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {
        "DBClusters": [{"DBClusterParameterGroup": "default.aurora-postgresql15"}]
    }

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds):
        result = simulate_parameter_change_impl(
            mock_cache, cluster_id="prod-pg-1", parameter_name="shared_buffers", new_value="8GB"
        )

    assert result["requires_restart"] is True
    assert result["is_dynamic"] is False
    assert "static fallback" in result["data_source"]
    # We must never have read parameters from a default.* group.
    rds.describe_db_cluster_parameters.assert_not_called()


def test_simulate_static_fallback_when_parameter_absent():
    """Parameter missing from a custom group -> static fallback, no crash."""
    mock_cache = MagicMock()
    rds = _rds_with_params("custom-pg15", [{"ParameterName": "autovacuum", "ApplyType": "dynamic"}])

    with patch(f"{MODULE}.rds_client_for_cluster", return_value=rds):
        result = simulate_parameter_change_impl(
            mock_cache, cluster_id="prod-pg-1", parameter_name="work_mem", new_value="256MB"
        )

    assert "static fallback" in result["data_source"]
    assert result["is_dynamic"] is True


def test_query_result_model_importable():
    """The shared QueryResult model is available for tools that return rows;
    a trivial smoke check keeps the import wired per the test brief."""
    qr = QueryResult(columns=["p"], rows=[{"p": "work_mem"}], row_count=1)
    assert qr.row_count == 1
