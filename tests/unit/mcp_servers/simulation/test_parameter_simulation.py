from unittest.mock import MagicMock

from mcp_servers.simulation.tools.parameter_simulation import simulate_parameter_change_impl


def test_simulate_dynamic_parameter():
    mock_cache = MagicMock()

    result = simulate_parameter_change_impl(mock_cache, cluster_id="prod-pg-1", parameter_name="work_mem", new_value="256MB")

    assert result["cluster_id"] == "prod-pg-1"
    assert result["parameter"] == "work_mem"
    assert result["new_value"] == "256MB"
    assert result["is_dynamic"] is True
    assert result["requires_restart"] is False
    assert result["impact_area"] == "memory"


def test_simulate_static_parameter():
    mock_cache = MagicMock()

    result = simulate_parameter_change_impl(mock_cache, cluster_id="prod-pg-1", parameter_name="shared_buffers", new_value="8GB")

    assert result["requires_restart"] is True
    assert result["is_dynamic"] is False
    assert "재시작" in result["recommendation"]
