from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.execute_sql import execute_sql_impl


def test_execute_sql_dangerous_blocked():
    mock_cache = MagicMock()
    result = execute_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="DROP TABLE users")
    assert result["status"] == "blocked"
    assert "force=true" in result["reason"]


def test_execute_sql_non_select_requires_approval():
    mock_cache = MagicMock()
    result = execute_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="UPDATE users SET name='test'")
    assert result["status"] == "approval_required"


def test_execute_sql_truncate_blocked():
    mock_cache = MagicMock()
    result = execute_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="TRUNCATE TABLE users")
    assert result["status"] == "blocked"


def test_execute_sql_delete_blocked():
    mock_cache = MagicMock()
    result = execute_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="DELETE FROM users WHERE id=1")
    assert result["status"] == "blocked"


@patch.dict("os.environ", {"TARGET_CLUSTER_ARN": "arn:test", "TARGET_SECRET_ARN": "arn:secret", "TARGET_DB_NAME": "testdb"})
@patch("mcp_servers.operations.tools.execute_sql.boto3")
def test_execute_sql_select_executes(mock_boto3):
    mock_rds_data = MagicMock()
    mock_boto3.client.return_value = mock_rds_data
    mock_rds_data.execute_statement.return_value = {
        "columnMetadata": [{"name": "id"}, {"name": "name"}],
        "records": [[{"longValue": 1}, {"stringValue": "alice"}]],
    }
    mock_cache = MagicMock()
    result = execute_sql_impl(mock_cache, cluster_id="prod-pg-1", sql="SELECT * FROM users")
    assert result["status"] == "executed"
    assert result["columns"] == ["id", "name"]
    assert result["row_count"] == 1


def test_execute_sql_explain_is_safe():
    """EXPLAIN statements should be treated as safe (no approval needed)."""
    mock_cache = MagicMock()
    # EXPLAIN needs boto3, so we just verify it doesn't return approval_required or blocked
    # by checking that it attempts execution (which will fail without AWS, but the logic path is correct)
    # Instead, test the pattern matching indirectly
    import re

    from mcp_servers.operations.tools.execute_sql import SAFE_PATTERNS
    assert any(re.match(p, "EXPLAIN SELECT * FROM users") for p in SAFE_PATTERNS)
