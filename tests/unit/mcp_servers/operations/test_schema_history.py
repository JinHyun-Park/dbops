from unittest.mock import MagicMock
from mcp_servers.operations.tools.schema_history import get_schema_history_impl
from mcp_servers.shared.models import QueryResult


def test_schema_history_with_changes():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["snapshot_time", "schema_name", "changes"],
        rows=[
            {"snapshot_time": "2024-01-02T00:00:00Z", "schema_name": "public", "changes": '{"added": ["orders"]}'},
            {"snapshot_time": "2024-01-01T00:00:00Z", "schema_name": "public", "changes": '{"added": ["users"]}'},
        ],
        row_count=2,
    )
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1", days=7)
    assert result["cluster_id"] == "prod-pg-1"
    assert result["period_days"] == 7
    assert result["count"] == 2
    assert len(result["changes"]) == 2


def test_schema_history_no_changes():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    result = get_schema_history_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["count"] == 0
    assert result["period_days"] == 30
