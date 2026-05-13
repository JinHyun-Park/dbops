from unittest.mock import MagicMock

from mcp_servers.incident.tools.similar_incidents import find_similar_incidents_impl


def test_similar_incidents_stub():
    mock_cache = MagicMock()
    result = find_similar_incidents_impl(
        mock_cache,
        cluster_id="prod-pg-1",
        symptoms="high CPU and connection spike",
    )
    assert result["cluster_id"] == "prod-pg-1"
    assert result["symptoms"] == "high CPU and connection spike"
    assert isinstance(result["similar_incidents"], list)
    assert "note" in result
