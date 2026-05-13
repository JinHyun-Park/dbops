from unittest.mock import MagicMock

from mcp_servers.operations.tools.schema_diff import get_schema_diff_impl
from mcp_servers.shared.models import QueryResult


def test_schema_diff_with_two_snapshots():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["schema_name", "tables_before", "tables_after"],
        rows=[
            {"schema_name": "public", "tables_before": '{"users": 5}', "tables_after": '{"users": 6}'},
        ],
        row_count=1,
    )
    result = get_schema_diff_impl(mock_cache, cluster_id="prod-pg-1", snapshot_a="2024-01-01T00:00:00Z", snapshot_b="2024-01-02T00:00:00Z")
    assert result["cluster_id"] == "prod-pg-1"
    assert result["count"] == 1
    assert len(result["diffs"]) == 1
    call_sql = mock_cache.execute.call_args[0][0]
    assert "snapshot_a" in call_sql or ":snapshot_a" in call_sql


def test_schema_diff_latest():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["schema_name", "diff"],
        rows=[{"schema_name": "public", "diff": '{"added": ["new_table"]}'}],
        row_count=1,
    )
    result = get_schema_diff_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["count"] == 1
    assert result["cluster_id"] == "prod-pg-1"


def test_schema_diff_no_changes():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    result = get_schema_diff_impl(mock_cache, cluster_id="prod-pg-1")
    assert result["count"] == 0
    assert result["diffs"] == []
